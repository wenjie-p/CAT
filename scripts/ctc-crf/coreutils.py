"""
Copyright 2021 Tsinghua University
Apache 2.0.
Author: Zheng Huahuan (zhh20@mails.tsinghua.edu.cn)
"""

import os
import argparse
import time
import json
import math
import shutil
import scheduler
import numpy as np
from collections import OrderedDict
from monitor import plot_monitor
from _specaug import SpecAug
from typing import Callable, Union, Sequence, Iterable

import torch
import torch.nn as nn
import torch.distributed as dist
from torch.nn.utils.rnn import pad_sequence


class Manager(object):
    def __init__(self, logger: OrderedDict, func_build_model: Callable[[argparse.Namespace, dict], Union[nn.Module, nn.parallel.DistributedDataParallel]], args: argparse.Namespace):
        super().__init__()

        with open(args.config, 'r') as fi:
            configures = json.load(fi)  # type: dict

        self.model = func_build_model(args, configures)

        # Initial specaug module
        if 'specaug_config' not in configures:
            specaug = None
            if args.rank == 0:
                highlight_msg("Disable SpecAug")
        else:
            specaug = SpecAug(**configures['specaug_config'])
            specaug = specaug.to(f'cuda:{args.gpu}')

        self.specaug = specaug

        # Initial scheduler and optimizer
        self.scheduler = GetScheduler(
            configures['scheduler'], self.model.parameters())

        self.log = logger
        self.rank = args.rank
        self.DEBUG = args.debug

        if args.resume is not None:
            print(f"[GPU {args.rank}]: Resuming from: {args.resume}")
            loc = f'cuda:{args.gpu}'
            checkpoint = torch.load(
                args.resume, map_location=loc)  # type: OrderedDict
            self.load(checkpoint)

    def run(self, train_sampler: torch.utils.data.distributed.DistributedSampler, trainloader: torch.utils.data.DataLoader, testloader: torch.utils.data.DataLoader, args: argparse.Namespace):

        epoch = self.scheduler.epoch_cur
        self.model.train()
        while True:
            epoch += 1
            train_sampler.set_epoch(epoch)

            train(trainloader, epoch, args, self)

            self.model.eval()
            metrics = test(testloader, args, self)
            if isinstance(metrics, tuple):
                # defaultly use the first one to evaluate
                metrics = metrics[0]
            state, info = self.scheduler.step(epoch, metrics)

            if args.gpu == 0:
                print(info)

            self.model.train()
            if self.rank == 0 and not self.DEBUG:
                self.log_export(args.ckptpath)
                plot_monitor(args.ckptpath)

            if state == 2:
                print("Terminated: GPU[%d]" % self.rank)
                dist.barrier()
                break
            elif self.rank != 0 or self.DEBUG:
                continue
            elif state == 0 or state == 1:
                self.save("checkpoint", args.ckptpath)
                if state == 1:
                    shutil.copyfile(
                        f"{args.ckptpath}/checkpoint.pt", f"{args.ckptpath}/bestckpt.pt")
            else:
                raise ValueError(f"Unknown state: {state}.")
            torch.cuda.empty_cache()

    def save(self, name: str, PATH: str = '') -> str:
        """Save checkpoint.

        The checkpoint file would be located at `PATH/name.pt`
        or `name.pt` if `PATH` is empty.
        """

        PATH = os.path.join(PATH, name+'.pt')
        torch.save(OrderedDict({
            'model': self.model.state_dict(),
            'scheduler': self.scheduler.state_dict(),
            'log': OrderedDict(self.log)
        }), PATH)
        return PATH

    def load(self, checkpoint: OrderedDict):
        r'Load checkpoint.'

        dist.barrier()
        self.model.load_state_dict(checkpoint['model'])
        self.scheduler.load_state_dict(checkpoint['scheduler'])
        self.log = checkpoint['log']

    def log_update(self, msg: list = [], loc: str = "log_train"):
        self.log[loc].append(msg)

    def log_export(self, PATH: str):
        """Save log file in {PATH}/{key}.csv
        """

        for key, value in self.log.items():

            with open(f"{PATH}/{key}.csv", 'w+', encoding='utf8') as file:
                data = [','.join([str(x) for x in infos])
                        for infos in value[1:]]
                file.write(value[0] + '\n' + '\n'.join(data))


def GetScheduler(scheduler_configs: dict, param_list: Iterable) -> scheduler.Scheduler:
    schdl_base = getattr(scheduler, scheduler_configs['type'])
    return schdl_base(scheduler_configs['optimizer'], param_list, **scheduler_configs['kwargs'])


def pad_list(xs: torch.Tensor, pad_value=0, dim=0) -> torch.Tensor:
    """Perform padding for the list of tensors.

    Args:
        xs (`list`): List of Tensors [(T_1, `*`), (T_2, `*`), ..., (T_B, `*`)].
        pad_value (float): Value for padding.

    Returns:
        Tensor: Padded tensor (B, Tmax, `*`).

    Examples:
        >>> x = [torch.ones(4), torch.ones(2), torch.ones(1)]
        >>> x
        [tensor([1., 1., 1., 1.]), tensor([1., 1.]), tensor([1.])]
        >>> pad_list(x, 0)
        tensor([[1., 1., 1., 1.],
                [1., 1., 0., 0.],
                [1., 0., 0., 0.]])

    """
    if dim == 0:
        return pad_sequence(xs, batch_first=True, padding_value=pad_value)
    else:
        xs = [x.transpose(0, dim) for x in xs]
        padded = pad_sequence(xs, batch_first=True, padding_value=pad_value)
        return padded.transpose(1, dim+1).contiguous()


def str2num(src: str) -> Sequence[int]:
    return list(src.encode())


def num2str(num_list: list) -> str:
    return bytes(num_list).decode()


def gather_all_gpu_info(local_gpuid: int, num_all_gpus: int = None) -> Sequence[int]:
    """Gather all gpu info based on DDP backend

    This function is supposed to be invoked in all sub-process.
    """
    if num_all_gpus is None:
        num_all_gpus = dist.get_world_size()

    gpu_info = torch.cuda.get_device_name(local_gpuid)
    gpu_info_len = torch.tensor(len(gpu_info)).cuda(local_gpuid)
    dist.all_reduce(gpu_info_len, op=dist.ReduceOp.MAX)
    gpu_info_len = gpu_info_len.cpu()
    gpu_info = gpu_info + ' ' * (gpu_info_len-len(gpu_info))

    unicode_gpu_info = torch.tensor(
        str2num(gpu_info), dtype=torch.uint8).cuda(local_gpuid)
    info_list = [torch.empty(
        gpu_info_len, dtype=torch.uint8, device=local_gpuid) for _ in range(num_all_gpus)]
    dist.all_gather(info_list, unicode_gpu_info)
    return [num2str(x.tolist()).strip() for x in info_list]


def gen_readme(path: str, model: nn.Module, gpu_info: list = []) -> str:
    if os.path.exists(path):
        highlight_msg(f"Not generate new readme, since '{path}' exists")
        return path

    model_size = count_parameters(model)/1e6

    msg = [
        "### Basic info",
        "",
        "**This part is auto generated, add your details in Appendix**",
        "",
        "* Model size/M: {:.2f}".format(model_size),
        f"* GPU info \[{len(gpu_info)}\]"
    ]
    gpu_set = list(set(gpu_info))
    gpu_set = {x: gpu_info.count(x) for x in gpu_set}
    gpu_msg = [f"  * \[{num_device}\] {device_name}" for device_name,
               num_device in gpu_set.items()]

    msg += gpu_msg + [""]
    msg += [
        "### Appendix",
        "",
        "* ",
        ""
    ]
    msg += [
        "### WER"
        "",
        "```",
        "",
        "```",
        "",
        "### Monitor figure",
        "![monitor](./ckpt/monitor.png)",
        ""
    ]
    with open(path, 'w') as fo:
        fo.write('\n'.join(msg))

    return path


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


class ProgressMeter(object):
    def __init__(self, num_batches, meters, prefix=""):
        self.batch_fmtstr = self._get_batch_fmtstr(num_batches)
        self.meters = meters
        self.prefix = prefix

    def display(self, batch):
        entries = [self.prefix + self.batch_fmtstr.format(batch)]
        entries += [str(meter) for meter in self.meters]
        print('\t'.join(entries))

    def _get_batch_fmtstr(self, num_batches):
        num_digits = len(str(num_batches // 1))
        fmt = '{:' + str(num_digits) + 'd}'
        return '[' + fmt + '/' + fmt.format(num_batches) + ']'


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self, name, fmt=':f'):
        self.name = name
        self.fmt = fmt
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)


def highlight_msg(msg: Union[Sequence[str], str]):
    if isinstance(msg, str):
        print("\n>>> {} <<<\n".format(msg))
        return

    try:
        terminal_col = os.get_terminal_size().columns
    except:
        terminal_col = 200
    max_len = terminal_col-4
    if max_len <= 0:
        print(msg)
        return None

    len_msg = max([len(line) for line in msg])

    if len_msg > max_len:
        len_msg = max_len
        new_msg = []
        for line in msg:
            if len(line) > max_len:
                _cur_msg = [line[i*max_len:(i+1)*max_len]
                            for i in range(len(line)//max_len+1)]
                new_msg += _cur_msg
            else:
                new_msg.append(line)
        del msg
        msg = new_msg

    for i, line in enumerate(msg):
        right_pad = len_msg-len(line)
        msg[i] = '# ' + line + right_pad*' ' + ' #'
    msg = '\n'.join(msg)

    msg = '\n' + "#"*(len_msg + 4) + '\n' + msg
    msg += '\n' + "#"*(len_msg + 4) + '\n'
    print(msg)


def train(trainloader, epoch: int, args: argparse.Namespace, manager: Manager):
    @torch.no_grad()
    def _cal_real_loss(loss, path_weight):
        if args.iscrf:
            partial_loss = loss.cpu()
            weight = torch.mean(path_weights)
            return partial_loss - weight
        else:
            return loss.cpu()

    scheduler = manager.scheduler

    model = manager.model
    optimizer = scheduler.optimizer

    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    losses_real = AverageMeter('Loss_real', ':.4e')
    progress = ProgressMeter(
        len(trainloader),
        [batch_time, data_time, losses, losses_real],
        prefix="Epoch: [{}]".format(epoch))

    end = time.time()
    fold = args.grad_accum_fold
    assert fold >= 1
    pre_steps = int(math.ceil(len(trainloader)/float(fold)) * (epoch-1))

    optimizer.zero_grad()
    for i, minibatch in enumerate(trainloader):
        # measure data loading time
        logits, input_lengths, labels, label_lengths, path_weights = minibatch
        logits, labels, input_lengths, label_lengths = logits.cuda(
            args.gpu, non_blocking=True), labels, input_lengths, label_lengths

        if manager.specaug is not None:
            logits, input_lengths = manager.specaug(logits, input_lengths)

        data_time.update(time.time() - end)

        # update every fold times and won't drop the last batch
        if fold == 1 or (i+1) % fold == 0 or (i+1) == len(trainloader):
            loss = model(logits, labels, input_lengths, label_lengths)
            loss.backward()
            real_loss = _cal_real_loss(loss, path_weights)

            # for Adam optimizer, even though fold > 1, it's no need to normalize grad
            # if using SGD, let grad = grad_accum / fold as following or use a new_lr = init_lr / fold
            # if fold > 1:
            #     for param in model.parameters():
            #         if param.requires_grad:
            #             param.grad.data /= fold
            optimizer.step()
            optimizer.zero_grad()
            scheduler.update_lr(pre_steps + (i + 1)/fold)

            # measure accuracy and record loss; item() can sync all processes.
            tolog = [loss.item(), real_loss.item(),
                     logits.size(0), time.time()-end]
            end = time.time()
            losses.update(tolog[0], tolog[2])
            losses_real.update(tolog[1], tolog[2])
            # measure elapsed time
            batch_time.update(tolog[-1])
            manager.log_update(
                [epoch, tolog[0], tolog[1], scheduler.lr_cur, tolog[-1]], loc='log_train')

            if ((i+1)/fold % args.print_freq == 0 or args.debug) and args.gpu == 0:
                progress.display(i+1)

            if args.debug and (i+1)/fold >= 20:
                if args.gpu == 0:
                    highlight_msg("In debugging, quit loop")
                dist.barrier()
                break
        else:
            # gradient accumulation w/o sync
            with model.no_sync():
                loss = model(logits, labels, input_lengths, label_lengths)
                loss.backward()


@torch.no_grad()
def test(testloader, args: argparse.Namespace, manager: Manager) -> float:

    model = manager.model

    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses_real = AverageMeter('Loss_real', ':.4e')
    progress = ProgressMeter(
        len(testloader),
        [batch_time, data_time, losses_real],
        prefix='Test: ')

    beg = time.time()
    end = time.time()
    for i, minibatch in enumerate(testloader):
        if args.debug and i >= 20:
            if args.gpu == 0:
                highlight_msg("In debugging, quit loop")
            dist.barrier()
            break
        # measure data loading time
        logits, input_lengths, labels, label_lengths, path_weights = minibatch
        logits, labels, input_lengths, label_lengths = logits.cuda(
            args.gpu, non_blocking=True), labels, input_lengths, label_lengths
        path_weights = path_weights.cuda(args.gpu, non_blocking=True)

        data_time.update(time.time() - end)

        loss = model(logits, labels, input_lengths, label_lengths)

        if args.iscrf:
            weight = torch.mean(path_weights)
            real_loss = loss - weight
        else:
            real_loss = loss

        dist.all_reduce(real_loss, dist.ReduceOp.SUM)
        real_loss = real_loss / dist.get_world_size()

        # measure accuracy and record loss
        losses_real.update(real_loss.item(), logits.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)

        end = time.time()

        if ((i+1) % args.print_freq == 0 or args.debug) and args.gpu == 0:
            progress.display(i+1)

    manager.log_update(
        [losses_real.avg, time.time() - beg], loc='log_eval')

    return losses_real.avg


def BasicDDPParser(istraining: bool = True, prog: str = '') -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    if istraining:
        parser.add_argument('-p', '--print-freq', default=10, type=int,
                            metavar='N', help='print frequency (default: 10)')
        parser.add_argument('--batch_size', default=256, type=int, metavar='N',
                            help='mini-batch size (default: 256), this is the total '
                            'batch size of all GPUs on the current node when '
                            'using Distributed Data Parallel')
        parser.add_argument("--seed", type=int, default=0,
                            help="Manual seed.")
        parser.add_argument("--grad-accum-fold", type=int, default=1,
                            help="Utilize gradient accumulation for K times. Default: K=1")

        parser.add_argument("--debug", action="store_true",
                            help="Configure to debug settings, would overwrite most of the options.")

        parser.add_argument("--data", type=str, default=None,
                            help="Location of training/testing data.")
        parser.add_argument("--trset", type=str, default=None,
                            help="Location of training data. Default: <data>/[pickle|hdf5]/tr.[pickle|hdf5]")
        parser.add_argument("--devset", type=str, default=None,
                            help="Location of dev data. Default: <data>/[pickle|hdf5]/cv.[pickle|hdf5]")
        parser.add_argument("--dir", type=str, default=None, metavar='PATH',
                            help="Directory to save the log and model files.")

    parser.add_argument("--config", type=str, default=None, metavar='PATH',
                        help="Path to configuration file of backbone.")
    parser.add_argument("--resume", type=str, default=None,
                        help="Path to location of checkpoint.")

    parser.add_argument('-j', '--workers', default=1, type=int, metavar='N',
                        help='number of data loading workers (default: 1)')
    parser.add_argument('--rank', default=0, type=int,
                        help='node rank for distributed training')
    parser.add_argument('--dist-url', default='tcp://127.0.0.1:12947', type=str,
                        help='url used to set up distributed training')
    parser.add_argument('--dist-backend', default='nccl', type=str,
                        help='distributed backend')
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of nodes for distributed training')
    parser.add_argument('--gpu', default=None, type=int,
                        help='GPU id to use.')

    return parser


def SetRandomSeed(seed: int = 0):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    torch.backends.cudnn.deterministic = True

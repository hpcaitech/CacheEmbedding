from dataclasses import dataclass, field
from typing import List, Optional
from time import perf_counter
import psutil
from contextlib import contextmanager

import torch
import torch.distributed as dist
from colossalai.utils import Timer
from colossalai.context.parallel_mode import ParallelMode

from recsys import DISTMGR

def get_mem_info(prefix=''):
    return f'{prefix}GPU memory usage: {torch.cuda.memory_allocated() / 1024**3:.2f} GB, ' \
           f'CPU memory usage: {psutil.Process().memory_info().rss / 1024**3:.2f} GB'

def count_parameters(model,prefix=''): 
    cnt = sum(p.numel() for p in model.parameters() if p.requires_grad)
    return f'{prefix}: {cnt}'


def trace_handler(p):
    stats = p.key_averages()
    if dist.get_rank() == 0:
        stats_str = "CPU Time Total:\n" + f"{stats.table(sort_by='cpu_time_total', row_limit=20)}" + "\n"
        stats_str += "Self CPU Time Total:\n" + f"{stats.table(sort_by='self_cpu_time_total', row_limit=20)}" + "\n"
        stats_str += "CUDA Time Total:\n" + f"{stats.table(sort_by='cuda_time_total', row_limit=20)}" + "\n"
        stats_str += "Self CUDA Time Total:\n" + f"{stats.table(sort_by='self_cuda_time_total', row_limit=20)}" + "\n"

        print(stats_str)
        # p.export_chrome_trace("tmp/trace_" + str(p.step_num) + ".json")


@contextmanager
def compute_throughput(batch_size) -> float:
    start = perf_counter()
    yield lambda : batch_size / ((perf_counter() - start)*1000)


@contextmanager
def get_time_elapsed(logger, repr: str):
    timer = Timer()
    timer.start()
    yield
    elapsed = timer.stop()
    logger.info(f"Time elapsed for {repr}: {elapsed:.4f}s", ranks=[0])


def get_world_size():
    return DISTMGR.get_world_size()


def get_rank():
    return DISTMGR.get_rank()


def get_group():
    return DISTMGR.get_group(ParallelMode.DATA)


def get_cpu_group():
    return DISTMGR.get_cpu_group(ParallelMode.DATA)


@dataclass
class TrainValTestResults:
    val_accuracies: List[float] = field(default_factory=list)
    val_aurocs: List[float] = field(default_factory=list)
    test_accuracy: Optional[float] = None
    test_auroc: Optional[float] = None
    

class EarlyStopper:
    def __init__(self, patience=7, verbose=False, delta=0, trace_func=print):
        self.patience = patience
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.delta = delta
        self.trace_func = trace_func

    def __call__(self, auc_score):

        if self.best_score is None:
            self.best_score = auc_score
        elif auc_score < self.best_score + self.delta:
            self.counter += 1
            if self.verbose:
                self.trace_func(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = auc_score
            self.counter = 0

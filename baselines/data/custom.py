import argparse
import torch
from torch import distributed as dist
import numpy as np
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple, Union
from colossalai.nn.parallel.layers.cache_embedding import FreqAwareEmbeddingBag
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor
from fbgemm_gpu.split_table_batched_embeddings_ops import SplitTableBatchedEmbeddingBagsCodegen, EmbeddingLocation, ComputeDevice, CacheAlgorithm
import time
import numpy as np
import torch
from torch.utils.data import IterableDataset
from torchrec.datasets.utils import PATH_MANAGER_KEY, Batch
from torchrec.datasets.criteo import BinaryCriteoUtils
from torchrec.sparse.jagged_tensor import KeyedJaggedTensor
from torch.utils.data import DataLoader, IterableDataset
import os

NUM_ROWS = int(2**25) # samples num
CAT_FEATURE_COUNT = 8
E = int(1e7) # unique embeddings num
s = 0.25 # long-tail skew

NUM_EMBEDDINGS_PER_FEATURE = (str(E) + ', ') * (CAT_FEATURE_COUNT - 1)
NUM_EMBEDDINGS_PER_FEATURE += str(E)
DEFAULT_LABEL_NAME = "click"
DEFAULT_INT_NAMES = ['rand_dense']
DEFAULT_CAT_NAMES = ["cat_{}".format(i) for i in range(CAT_FEATURE_COUNT)]


TOTAL_TRAINING_SAMPLES = NUM_ROWS

class CustomIterDataPipe(IterableDataset):
    def __init__(
        self,
        batch_size: int,
        rank: int,
        world_size: int,
        shuffle_batches: bool = False,
        mmap_mode: bool = False,
        hashes: Optional[List[int]] = None,
        path_manager_key: str = PATH_MANAGER_KEY,
    ) -> None:
        self.batch_size = batch_size
        self.rank = rank
        self.world_size = world_size
        self.shuffle_batches = shuffle_batches
        self.mmap_mode = mmap_mode
        self.hashes = hashes
        self.path_manager_key = path_manager_key
        self.keys: List[str] = DEFAULT_CAT_NAMES
        self._num_ids_in_batch: int = CAT_FEATURE_COUNT * batch_size
        self.lengths: torch.Tensor = torch.ones((self._num_ids_in_batch,), dtype=torch.int32)
        self.offsets: torch.Tensor = torch.arange(0, self._num_ids_in_batch + 1, dtype=torch.int32)
        self.num_batches = NUM_ROWS // self.batch_size // self.world_size
        self.length_per_key: List[int] = CAT_FEATURE_COUNT * [batch_size]
        self.offset_per_key: List[int] = [batch_size * i for i in range(CAT_FEATURE_COUNT + 1)]
        self.index_per_key: Dict[str, int] = {key: i for (i, key) in enumerate(self.keys)}
        self.stride = batch_size
        # random generation
        self.min_sample = (1 / E) ** s
        self.max_sample = 1.
        
        self.iter_count = 0
    def __len__(self) -> int:
        return self.num_batches
    
    def _sampler(self, rand_float):
        sample_float = rand_float * (self.max_sample - self.min_sample) + self.min_sample
        return torch.floor(1 / (sample_float ** (1 / s))).long() - 1
    def _generate_indices(self, length):
        indices = self._sampler(torch.rand(length,))
        return indices
    
    def __iter__(self) -> Iterator[Batch]:
        while self.iter_count < self.num_batches:
            indices = self._generate_indices(self.batch_size * CAT_FEATURE_COUNT)
            # print(indices)
            self.iter_count += 1
            yield self._make_batch(indices)
            
    def _make_batch(self, indices):
        ret = Batch(
            dense_features=torch.rand(self.stride, 1),
            sparse_features=KeyedJaggedTensor(
                keys=self.keys,
                values=indices,
                lengths=self.lengths,
                offsets=self.offsets,
                stride=self.stride,
                length_per_key=self.length_per_key,
                offset_per_key=self.offset_per_key,
                index_per_key=self.index_per_key,
            ),
            labels=torch.randint(2, (self.stride,))
        )
        return ret
    
def get_custom_data_loader(
    args: argparse.Namespace,
    stage: str) -> DataLoader:
    rank = dist.get_rank()
    world_size = dist.get_world_size()
    if stage == "train":
        dataloader = DataLoader(
            CustomIterDataPipe(
                batch_size=args.batch_size,
                rank=rank,
                world_size=world_size,
                shuffle_batches=args.shuffle_batches,
                hashes=None
            ),
            batch_size=None,
            pin_memory=args.pin_memory,
            collate_fn=lambda x: x,
        )
    else :
        dataloader = []
    return dataloader

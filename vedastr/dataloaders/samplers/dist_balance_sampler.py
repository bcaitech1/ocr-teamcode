import copy
import logging
import math
import random

import numpy as np
from torch.utils.data import DistributedSampler

from .registry import DISTSAMPLER
from ...utils import get_dist_info


@DISTSAMPLER.register_module
class BalanceSampler(DistributedSampler):
    """
    Balance Sampler for Dataloader. Based on a given ratio, BalanceSampler
    provide an iterable over the index of given dataset.
    Arguments:
        dataset (Dataset): In this project, the dataset should be an instance
                          of vedastr.dataset.ConcarDataset. You can implement
                          a dataset which should have attr: data_range,
                          batch_ratio by yourself.
        batch_size (int):  how many samplers per batch
        shuffle (bool): The indices of each dataset will be shuffled
                       at each epoch.
        oversample (bool): Set to True to oversample smaller sampler set.
        downsample (bool): Set to True to downsample bigger sampler set.
        shuffle_batch (bool): If true, it will shuffle the order in the batch.
    .. warning:: If both oversample and downsample is True, BanlanceSampler
                will do oversample first. That means downsample will do
                no effect.
                 The last batch mey have different batch ratio, which means
                last batch may be not balance.
    """

    def __init__(self,
                 dataset,
                 samples_per_gpu: int,
                 shuffle: bool,
                 oversample: bool = False,
                 downsample: bool = False,
                 shuffle_batch: bool = False,
                 eps: float = 0.1,
                 seed=0,
                 drop_last=False,
                 ):
        assert hasattr(dataset, 'data_range')
        assert hasattr(dataset, 'batch_ratio')
        rank, num_replicas = get_dist_info()
        if seed is None:
            seed = 0
        self.dataset = dataset
        self.samples_range = dataset.data_range
        self.batch_ratio = np.array(dataset.batch_ratio)
        self.batch_size = samples_per_gpu * num_replicas # total batch size
        self.batch_sizes = self._compute_each_batch_size()
        self.shuffle_batch = shuffle_batch
        self.drop_last = drop_last  # noqa 501
        new_br = self.batch_sizes / self.batch_size
        br_diffs = np.abs((new_br - self.batch_ratio))
        assert not np.sum(br_diffs > eps), \
            "After computing the batch sizes of each dataset based on" \
            "given batch ratio, the max difference between new batch ratio " \
            "which compute based on the computed batch size and" \
            f" given batch ratio is large than the eps {eps}.\n" \
            "Please Considering increase the value of eps or batch size." \
            f"Current computed batch sizes are {self.batch_sizes}, " \
            f"new batch ratios are {new_br}, while give batch ratio" \
            f" are {self.batch_ratio}.\n" \
            f"The max difference between given batch ratio and " \
            f"new batch ratio is {np.max(np.array(br_diffs))}."

        assert 0 not in self.batch_sizes, \
            "0 batch size is not supported, where batch " \
            "size is computed based on the batch ratio." \
            f" Computed batch size is {self.batch_sizes}."

        assert np.sum(self.batch_sizes) == self.batch_size
        logging.info(f"The truly used batch ratios are {new_br}")
        self.batch_ratio = new_br
        self.oversample = oversample
        self.downsample = downsample
        self.shuffle = shuffle
        super(BalanceSampler, self).__init__(dataset=dataset,
                                             rank=rank,
                                             num_replicas=num_replicas,
                                             shuffle=shuffle,
                                             seed=seed,
                                             drop_last=drop_last,
                                             )
        self._generate_indices()
        # If the dataset length is evenly divisible by # of replicas, then
        # there is no need to drop any data, since the dataset will be split
        # equally.
        if self.drop_last and self._num_samples % self.num_replicas != 0:
            # Split to nearest available length that is evenly divisible.
            # This is to ensure each rank receives the same amount of data when
            # using this Sampler.
            self.single_rank_num_samples = math.ceil(
                (self._num_samples - self.num_replicas) / self.num_replicas)
        else:
            self.single_rank_num_samples = math.ceil(self._num_samples /
                                                     self.num_replicas)
        self.total_size = self.single_rank_num_samples * self.num_replicas

    def _compute_each_batch_size(self):
        batch_sizes = self.batch_ratio * self.batch_size
        int_bs = batch_sizes.astype(np.int)
        float_bs = (batch_sizes - int_bs) >= 0.5
        diff = self.batch_size - np.sum(int_bs) - np.sum(float_bs)
        float_bs[np.where(float_bs == (diff < 0))[0][:int(abs(diff))]] = (
                diff >= 0)

        return (int_bs + float_bs).astype(np.int)

    @property
    def _num_samples(self):
        return self.num_samples

    @_num_samples.setter
    def _num_samples(self, v):
        self.num_samples = v

    def _generate_indices(self):
        self._num_samples = len(self.dataset)
        indices_ = []
        # TODO, elegant
        for idx, v in enumerate(self.samples_range):
            if idx == 0:
                temp = list(range(v))
                if self.shuffle:
                    np.random.shuffle(temp)
                indices_.append(temp)
            else:
                temp = list(range(self.samples_range[idx - 1], v))
                if self.shuffle:
                    np.random.shuffle(temp)
                indices_.append(temp)
        per_dataset_len = [len(index) for index in indices_]
        pratios = [l / s for (l, s) in zip(per_dataset_len, self.batch_sizes)]
        if self.oversample:
            need_len = [
                int(np.ceil(max(pratios) * size)) for size in self.batch_sizes
            ]
            indices_ = self._oversample(indices_, need_len)
        elif self.downsample:
            need_len = [
                int(np.ceil(min(pratios) * size)) for size in self.batch_sizes
            ]
            indices_ = self._downsample(indices_, need_len)
        return indices_

    def __iter__(self):

        # deterministically shuffle based on epoch and seed
        if self.shuffle or self.oversample or self.downsample:
            np.random.seed(self.seed + self.epoch)
            random.seed(self.seed + self.epoch)

        indices_ = self._generate_indices()
        total_nums = self.total_size // self.batch_size
        final_index = [total_nums * size for size in self.batch_sizes]
        indices = []
        for idx2 in range(total_nums):
            batch_indices = []
            for idx3, size in enumerate(self.batch_sizes):
                batch_indices.append(indices_[idx3][idx2 * size:(idx2 + 1) * size])  # noqa 501
            batch_indices = [l_index for bl in batch_indices for l_index in bl]  # noqa 501
            indices += batch_indices
        for idx4, index in enumerate(final_index):
            indices += indices_[idx4][index:]
        if self.total_size > len(indices):
            indices += indices[:self.total_size - len(indices)]
        else:
            indices = indices[:self.total_size]
        assert len(indices) == self.total_size

        # subsample
        indices = indices[self.rank:self.total_size:self.num_replicas]
        assert len(indices) == self.single_rank_num_samples

        return iter(indices)

    def _oversample(self, indices, need_len):
        result_indices = []
        for idx, index in enumerate(indices):
            current_nums = len(index)
            need_num = need_len[idx] - current_nums
            total_nums = need_num // current_nums
            mod_nums = need_num % current_nums
            init_index = copy.copy(index)
            for _ in range(max(0, total_nums)):
                new_index = copy.copy(init_index)
                if self.shuffle:
                    np.random.shuffle(new_index)
                index += new_index
            index += random.sample(index, mod_nums)
            result_indices.append(index)
        self._num_samples = np.sum(need_len)

        return result_indices

    def _downsample(self, indices, need_len):
        result_indices = []
        for idx, index in enumerate(indices):
            index = np.random.sample(index, need_len[idx])
            result_indices.append(index)
        self._num_samples = np.sum(need_len)
        return result_indices

    def __len__(self):
        return self.single_rank_num_samples

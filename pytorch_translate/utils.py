#!/usr/bin/env python3

import argparse
import collections
import itertools
import os
import signal
import threading
import time
from typing import List, Optional

import torch
from fairseq import distributed_utils, tasks, utils


# Helper type for argparse to enable flippable boolean flags. For example,
# group.add_argument("--foo", type=utils.bool_flag, nargs="?", const=True,
#                    default=False)
# creates a --foo flag that defaults to False and can be set to True by
# specifying `--foo True` or just `--foo`. Some other --bar flag that defaults
# to True can be set to False by specifying `--bar False`.
def bool_flag(value):
    if value.lower() in ("true", "t", "1"):
        return True
    elif value.lower() in ("false", "f", "0"):
        return False
    else:
        raise argparse.ArgumentTypeError(
            f"Expected boolean string such as 'true'/'false' instead of {value}."
        )


# The purpose of this class is to keep track of the list of checkpoints
# currently alive and automatically delete those that are no more required
# and that we do not want to keep around.
# In a nutshell, this class remembers the last max_num_checkpoints
# and delete (auto_clear == True) the oldest checkpoint each time a new one
# is added past this number.
# TODO(T34212782): replace this class with simple list of strings and helper fns
# to avoid messy pickling/unpickling of objects.
class ManagedCheckpoints:

    # - max_num_checkpoints: Maximum number of checkpoints we need at one point.
    # - auto_clear: Control whether or not checkpoints should get deleted when
    #   they are not in the last max_num_checkpoints appended to the
    #   self anymore.
    def __init__(self, max_num_checkpoints, auto_clear):
        self.auto_clear = auto_clear
        assert max_num_checkpoints > 0, "Empty listing is not supported"
        self.kept_checkpoints = collections.deque(maxlen=max_num_checkpoints)

    def __eq__(self, other):
        return (
            isinstance(other, ManagedCheckpoints)
            and self.auto_clear == other.auto_clear
            and self.kept_checkpoints == other.kept_checkpoints
        )

    def __repr__(self):
        return (
            f"ManagedCheckpoints(kept_checkpoints={self.kept_checkpoints}, "
            f"auto_clear={self.auto_clear})"
        )

    def append(self, checkpoint_filename):
        # If we append a filename that we already manage, we would need
        # to remove it from its current position otherwise it may get deleted
        # by the time we reach the use for this append.
        # E.g., Let us assume we have a max of 2 checkpoint.
        # We insert last_checkpoint, use it, then insert last_checkpoint,
        # use it, then insert it again. The first file gets delete, but it
        # is actually the same as the current one, so we actually delete
        # the current one. Then we try to use it and we will get an error
        # for file not found.
        # Although this is pretty easy to support this case, given we only
        # append the same file names with no_epoch_checkpoints, we decided
        # not to slow every other uses case for that.
        # Instead we rely on the fact that when this happens, we actually
        # don't automatically delete files (auto_clear == False).
        assert not self.auto_clear or not self.kept_checkpoints.count(
            checkpoint_filename
        ), "Not yet implemented"
        if (
            self.auto_clear
            and len(self.kept_checkpoints) == self.kept_checkpoints.maxlen
        ):
            # We reach the max number of checkpoints we keep around.
            # Delete the oldest one.
            try:
                os.remove(self.kept_checkpoints.popleft())
            except FileNotFoundError:
                pass
        # Save the new checkpoint.
        self.kept_checkpoints.append(checkpoint_filename)

    def get_last_n(self, num_elements):
        assert 0 < num_elements <= self.kept_checkpoints.maxlen, (
            f"Requested number of elements {num_elements} "
            f"must be between 0 and maxlen {self.kept_checkpoints.maxlen}, "
            f"exclusive"
        )
        # If we ask for more elements than what we currently have, return all
        # of them.
        # Reason why we don't assert unlike for maxlen is because maxlen points
        # out a design issue (the reserved size is too small), whereas the case
        # where we ask more elements than what is currently in the list happens
        # when we print the average of X checkpoints for BLEU, but we haven't
        # yet computed that many checkpoints. We could also assert in this case
        # and fix the caller, but handling it here was just fine!
        start = max(len(self.kept_checkpoints) - num_elements, 0)
        return collections.deque(itertools.islice(self.kept_checkpoints, start, None))


# Variation on the fairseq StopwatchMeter that separates statistics by number
# of tokens. Sentences longer than max_length are stored in the last bucket.
class BucketStopwatchMeter(object):
    def __init__(self, increment, max_length, sentences_per_batch):
        self.increment = increment
        self.n_buckets = max_length // increment + 1
        self.sentences_per_batch = sentences_per_batch
        self.reset()

    def start(self):
        self.start_time = time.time()

    def stop(self, n=1):
        if self.start_time is not None:
            delta = time.time() - self.start_time
            bucket_id = min(self.n_buckets - 1, n // self.increment)
            self.sum[bucket_id] += delta
            self.n[bucket_id] += n
            self.count[bucket_id] += 1
            self.start_time = None

    def reset(self):
        self.sum = [0] * self.n_buckets
        self.n = [0] * self.n_buckets
        self.count = [0] * self.n_buckets
        self.start_time = None

    def reset_bucket(self, bucket_id):
        if self.start_time is None:
            self.sum[bucket_id] = 0
            self.n[bucket_id] = 0
            self.count[bucket_id] = 0

    @property
    def avg(self):
        return sum(self.sum) / sum(self.n)

    @property
    def avgs(self):
        result = [0] * self.n_buckets
        for i in range(self.n_buckets):
            if self.n[i] != 0:
                result[i] = self.sum[i] / self.n[i]
            else:
                result[i] = 0
        return result


def load_diverse_ensemble_for_inference(
    filenames: List[str], task: Optional[tasks.FairseqTask] = None
):
    """Load an ensemble of diverse models for inference.

    This method is similar to fairseq.utils.load_ensemble_for_inference
    but allows to load diverse models with non-uniform args.

    Args:
        filenames: List of file names to checkpoints
        task: Optional[FairseqTask]. If this isn't provided, we setup the task
            using the first checkpoint's model args loaded from the saved state.

    Return:
        models, args: Tuple of lists. models contains the loaded models, args
            the corresponding configurations.
        task: Either the input task or the task created within this function
            using args
    """

    # load model architectures and weights
    checkpoints_data = []
    for filename in filenames:
        if not os.path.exists(filename):
            raise IOError("Model file not found: {}".format(filename))
        checkpoints_data.append(
            torch.load(
                filename,
                map_location=lambda s, l: torch.serialization.default_restore_location(
                    s, "cpu"
                ),
            )
        )
    # build ensemble
    ensemble = []
    if task is None:
        task = tasks.setup_task(checkpoints_data[0]["args"])
    for checkpoint_data in checkpoints_data:
        model = task.build_model(checkpoint_data["args"])
        model.load_state_dict(checkpoint_data["model"])
        ensemble.append(model)
    args_list = [s["args"] for s in checkpoints_data]
    return ensemble, args_list, task


def densify(t):
    """Removes holes in an array.

    This function converts a 1-dimensional tensor of length n without duplicates
    to a 1-dimensional tensor of the same length with all elements less than n
    while preserving the order. For example,

        [1, 0, 4, 5, 10, 9] -> [1, 0, 2, 3, 5, 4]
    """
    _, sorted_indices = torch.sort(t)
    _, dense_t = torch.sort(sorted_indices)
    return dense_t


def maybe_cat(tensors, dim, nullable=None):
    """Like torch.cat, but skips elements in `tensors` which are None.

    Args:
        tensors: List of tensors (compare torch.cat())
        dim: Dimension along which to concatenate (compare to torch.cat())
        nullable: List of the same length as `tensors`. If specified, throw
            a RuntimeError if the i-th element in `tensors` is None and the
            i-th element in nullable is False.

    Returns:
        Concatenation of all tensors in `tensors` along `dim` which are not
        None.

    Throws:
        RuntimeError is `nullable` constraint is violated or all alements in
        `tensors` are None.
    """
    if nullable is not None and any(
        (t is None) and not n for t, n in zip(tensors, nullable)
    ):
        raise RuntimeError("Unexpected element in tensors is None.")
    filtered = [t for t in tensors if t is not None]
    if len(filtered) == 1:
        return filtered[0]
    return torch.cat(filtered, dim=dim)


def maybe_cuda(t):
    """Calls `cuda()` on `t` if cuda is available."""
    if torch.cuda.is_available():
        return t.cuda()
    return t


def average_tensors(tensor_list, norm_fn=None, weights=None):
    """Averages a list of tensors.

    Average the elements in tensor_list as follows:
      w1*norm_fn(t1) + w2*norm_fn(t2) + ...
    The default behavior corresponds to a [weighted] mean. You can set norm_fn
    to F.softmax or F.log_softmax to average in probability or logprob space.

    Note: This implementation favours memory efficiency over numerical
    stability, and iterates through `tensor_list` in a Python for-loop rather
    than stacking it to a PyTorch tensor.

    Arguments:
        tensor_list (list): Python list of tensors of the same size and same type
        norm_fn (function): If set, apply norm_fn() to elements in `tensor_list`
            before averaging. If list of functions, apply n-th function to
            n-th tensor.
        weights (list): List of tensors or floats to use to weight models. Must
            be of the same length as `tensor_list`. If none, use uniform weights.

    Returns:
        Average of the tensors in `tensor_list`
    """
    n_tensors = len(tensor_list)
    if weights is None:
        weights = [1.0 / float(n_tensors)] * n_tensors
    if not isinstance(norm_fn, list):
        norm_fn = [norm_fn] * n_tensors
    assert n_tensors == len(weights)
    assert n_tensors == len(norm_fn)

    def id_fn(x, dim):
        return x

    norm_fn = [id_fn if f is None else f for f in norm_fn]
    acc = torch.zeros_like(tensor_list[0])
    for f, w, t in zip(norm_fn, weights, tensor_list):
        acc += w * f(t, dim=-1)
    return acc


def load_embedding(embedding, dictionary, pretrained_embed):
    """Loads pretrained embeddings.

    Loads pretrained embeddings into a nn.Embedding layer. pretrained_embed
    can either be a nn.Embedding layer, in which case the embedding is set
    to the pretrained_embed argument, or a path to an embedding file.

    Arguments:
        embedding (pytorch_translate.common_layers.Embedding):
            Embedding layer whose weights are to be set.
        dictionary (fairseq.data.dictionary.Dictionary): dictionary with the
            same vocabulary size as the embedding argument.
        pretrained_embed (Union(string, nn.Embedding)): source of the
            weights to be loaded.
    """
    if pretrained_embed is None:
        return

    if isinstance(pretrained_embed, torch.nn.Embedding):
        embedding.weight = pretrained_embed.weight
    else:
        embed_dict = utils.parse_embedding(pretrained_embed)
        utils.load_embedding(embed_dict, dictionary, embedding)

    embedding.init_normalization_if_needed()


def torch_find(index, query, vocab_size):
    """
    Finds elements of query from index, outputting the last (max) index for each
    query.
    preconditions:  (1) index and query are flat arrays (can be different sizes)
                    (2) all tokens in index and query have values < vocab_size
    """
    full_to_index = maybe_cuda(torch.zeros(vocab_size).long())
    index_shape_range = maybe_cuda(torch.arange(index.shape[0]).long())
    full_to_index[index] = index_shape_range
    result = full_to_index[query]
    return result


def all_gather_from_master(args, data: List) -> List:
    if args.distributed_world_size == 1:
        return data

    gathered_data = distributed_utils.all_gather_list(data)
    # Converts [[x0, y0, z0, ...], [x1, y1, z1, ...], [x2, y2, z2, ...], ...]
    # to [[x0, x1, x2, ...], [y0, y1, y2, ...], [z0, z1, z2, ...], ...]
    gathered_data_list = list(zip(*gathered_data))

    output_data = []
    for data_index, all_data in enumerate(gathered_data_list):
        # The master's (process 0) data is guaranteed to be in position 0.
        master_data = all_data[0]
        # Sanity check that only the master returned any result.
        if master_data is None:
            raise RuntimeError(
                f"Input data element {data_index} of all_gather_from_master "
                f"returned None from master. Results from all processes: {all_data}"
            )
        for i in range(1, len(all_data)):
            if all_data[i] is not None:
                raise RuntimeError(
                    f"Input data element {data_index} of all_gather_from_master "
                    f"should have returned None from non-master process {i}. "
                    f"Results from all processes: {all_data}"
                )
        output_data.append(master_data)
    return output_data

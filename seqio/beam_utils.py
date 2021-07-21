# Copyright 2021 The SeqIO Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SeqIO Beam utilities."""

import importlib
import json
from absl import logging
import apache_beam as beam
import apache_beam.metrics as metrics
import numpy as np
import seqio
import tensorflow.compat.v2 as tf


def _import_modules(modules):
  for module in modules:
    if module:
      importlib.import_module(module)


class PreprocessTask(beam.PTransform):
  """Abstract base class to preprocess a Task.

  Returns a PCollection of example dicts containing Tensors.
  """

  def __init__(
      self, task, split, modules_to_import=()):
    """BasePreprocessTask constructor.

    Args:
      task: Task, the task to process.
      split: string, the split to process.
      modules_to_import: (Optional) list, modules to import.
    """
    self._task = task
    self._split = split
    self._modules_to_import = modules_to_import
    self.shards = list(range(len(task.source.list_shards(split))))
    logging.info(
        "%s %s shards: %s", task.name, split, ", ".join(
            ["%s" % f for f in self.shards]))

  def _increment_counter(self, name):
    metrics.Metrics.counter(
        str("%s_%s" % (self._task.name, self._split)), name).inc()

  def _emit_examples(self, shard_index):
    """Emits examples keyed by shard number and index for a single shard."""
    _import_modules(self._modules_to_import)
    logging.info("Processing shard: %d", shard_index)
    self._increment_counter("input-shards")

    ds = self._task.source.get_dataset(
        split=self._split,
        shard_info=seqio.ShardInfo(
            index=shard_index, num_shards=len(self.shards)
        ),
        shuffle=False)

    ds = ds.prefetch(tf.data.AUTOTUNE)

    ds = self._task.preprocess_precache(ds)

    for i, ex in enumerate(ds.as_numpy_iterator()):
      self._increment_counter("examples")
      # Log every power of two.
      if i & (i - 1) == 0:
        logging.info("Example [%d] = %s", i, ex)
      yield ex

  def expand(self, pipeline):
    # The Reshuffles allow for better parallelism.
    return (pipeline
            | "create_shards" >> beam.Create(self.shards)
            | "shard_reshuffle" >> beam.Reshuffle()
            | "emit_examples" >> beam.FlatMap(self._emit_examples)
            | "example_reshuffle" >> beam.Reshuffle())


class WriteExampleTfRecord(beam.PTransform):
  """Writes examples (dicts) to a TFRecord of tf.Example protos."""

  def __init__(self, output_path, num_shards=None):
    """WriteExampleTfRecord constructor.

    Args:
      output_path: string, path to the output TFRecord file (w/o shard suffix).
      num_shards: (optional) int, number of shards to output or None to use
        liquid sharding.
    """
    self._output_path = output_path
    self._num_shards = num_shards

  def expand(self, pcoll):
    return (
        pcoll
        | beam.Map(seqio.dict_to_tfexample)
        | beam.Reshuffle()
        | beam.io.tfrecordio.WriteToTFRecord(
            self._output_path,
            num_shards=self._num_shards,
            coder=beam.coders.ProtoCoder(tf.train.Example)))


class WriteJson(beam.PTransform):
  """Writes datastructures to file as JSON(L)."""

  def __init__(self, output_path, prettify=True):
    """WriteJson constructor.

    Args:
      output_path: string, path to the output JSON(L) file.
      prettify: bool, whether to write the outputs with sorted keys and
        indentation. Note this not be used if there are multiple records being
        written to the file (JSONL).
    """
    self._output_path = output_path
    self._prettify = prettify

  def _jsonify(self, el):
    if self._prettify:
      return json.dumps(el, sort_keys=True, indent=2)
    else:
      return json.dumps(el)

  def expand(self, pcoll):
    return (
        pcoll
        | beam.Map(self._jsonify)
        | "write_info" >> beam.io.WriteToText(
            self._output_path,
            num_shards=1,
            shard_name_template=""))


class GetInfo(beam.PTransform):
  """Computes info for dataset examples.

  Expects a single PCollections of examples.
  Returns a dictionary with information needed to read the data (number of
  shards, feature shapes and types)
  """

  def __init__(self, num_shards):
    self._num_shards = num_shards

  def _info_dict(self, ex):
    if not ex:
      return {}
    assert len(ex) == 1
    ex = ex[0]
    info = {
        "num_shards": self._num_shards,
        "features": {},
        "seqio_version": seqio.__version__,
    }
    feature_dict = info["features"]
    for k, v in ex.items():
      t = tf.constant(v)
      dtype = t.dtype.name
      shape = [None] * len(t.shape)
      feature_dict[k] = {"shape": shape, "dtype": dtype}
    return info

  def expand(self, pcoll):
    return (
        pcoll
        | beam.combiners.Sample.FixedSizeGlobally(1)
        | beam.Map(self._info_dict))


class GetStats(beam.PTransform):
  """Computes stastistics for dataset examples.

  Expects a dictionary of string identifiers mapped to PCollections of examples.
  Returns a dictionary with statistics (number of examples, number of tokens)
  prefixed by the identifiers.
  """

  def __init__(self, output_features):
    self._output_features = output_features

  def expand(self, pcoll):
    to_dict = lambda x: {x[0]: x[1]}
    example_counts = (
        pcoll
        | "count_examples" >> beam.combiners.Count.Globally()
        | "key_example_counts" >> beam.Map(
            lambda x: ("examples", x))
        | "example_count_dict" >> beam.Map(to_dict))
    def _count_tokens(pcoll, feat):

      def _count(ex):
        if (feat in ex and isinstance(ex[feat], np.ndarray) and
            ex[feat].dtype in (np.int32, np.int64)):
          yield ("%s_tokens" % feat, int(sum(ex[feat] > 1)))

      return pcoll | "key_%s_toks" % feat >> beam.FlatMap(_count)

    token_counts = (
        [_count_tokens(pcoll, feat)
         for feat in self._output_features]
        | "flatten_tokens" >> beam.Flatten())
    total_tokens = (
        token_counts
        | "sum_tokens" >> beam.CombinePerKey(sum)
        | "token_count_dict" >> beam.Map(to_dict))
    max_tokens = (
        token_counts
        | "max_tokens" >> beam.CombinePerKey(max)
        | "rename_max_stat" >> beam.Map(
            lambda x: (x[0].replace("tokens", "max_tokens"), x[1]))
        | "token_max_dict" >> beam.Map(to_dict))

    def _merge_dicts(dicts):
      merged_dict = {}
      for d in dicts:
        assert not set(merged_dict).intersection(d)
        merged_dict.update(d)
      return merged_dict
    return (
        [example_counts, total_tokens, max_tokens]
        | "flatten_counts" >> beam.Flatten()
        | "merge_stats" >> beam.CombineGlobally(_merge_dicts))

# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================

"""Binary for training translation models and decoding from them.

Running this program without --decode will download the WMT corpus into
the directory specified as --data_dir and tokenize it in a very basic way,
and then start training a model saving checkpoints to --train_dir.

Running with --decode starts an interactive loop so you can see how
the current checkpoint translates English sentences into French.

See the following papers for more information on neural translation models.
 * http://arxiv.org/abs/1409.3215
 * http://arxiv.org/abs/1409.0473
 * http://arxiv.org/abs/1412.2007
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import math
import os
import sys
import time

import numpy as np
import tensorflow as tf
from six.moves import xrange  # pylint: disable=redefined-builtin

from seq2seq import data_utils
from seq2seq import seq2seq_model
from shi_gen_util import shi_util
from random import randint


tf.app.flags.DEFINE_float("learning_rate", 0.5, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.99,
                          "Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0,
                          "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer("batch_size", 64,
                            "Batch size to use during training.")
# default = 512, seems overfit, change to 256
tf.app.flags.DEFINE_integer("size", 300, "Size of each model layer.")
tf.app.flags.DEFINE_integer("num_layers", 2, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("from_vocab_size", 8000, "source sentence vocabulary size.")
tf.app.flags.DEFINE_integer("to_vocab_size", 8000, "target sentence vocabulary size.")

tf.app.flags.DEFINE_integer("steps_per_checkpoint", 200,
                            "How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_boolean("self_test", False,
                            "Run a self-test if this is set to True.")
tf.app.flags.DEFINE_boolean("use_fp16", False,
                            "Train using fp16 instead of fp32.")

# ./tem512 is for size 512 check potins, ./tmp256 is for 256 size
tf.app.flags.DEFINE_string("train_dir", "./tmp256", "Training directory.")


# tf.app.flags.DEFINE_string("data_dir", "/tmp", "Data directory")
# tf.app.flags.DEFINE_string("from_train_data", None, "Training data.")
# tf.app.flags.DEFINE_string("to_train_data", None, "Training data.")
# tf.app.flags.DEFINE_string("from_dev_data", None, "Training data.")
# tf.app.flags.DEFINE_string("to_dev_data", None, "Training data.")
# tf.app.flags.DEFINE_integer("max_train_data_size", 0,
#                             "Limit on the size of training data (0: no limit).")
# tf.app.flags.DEFINE_boolean("decode", False,
#                             "Set to True for interactive decoding.")

FLAGS = tf.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model.Seq2SeqModel for details of how they work.
_buckets = [(5, 6), (6, 7), (8, 9), (15, 16)]
buckets = _buckets

_show_example_num = 5


def create_model(session, forward_only):
    """Create translation model and initialize or load parameters in session."""
    dtype = tf.float16 if FLAGS.use_fp16 else tf.float32
    model = seq2seq_model.Seq2SeqModel(
        FLAGS.from_vocab_size,
        FLAGS.to_vocab_size,
        _buckets,
        FLAGS.size,
        FLAGS.num_layers,
        FLAGS.max_gradient_norm,
        FLAGS.batch_size,
        FLAGS.learning_rate,
        FLAGS.learning_rate_decay_factor,
        forward_only=forward_only,
        dtype=dtype)
    ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir,)
    if ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
        print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
        model.saver.restore(session, ckpt.model_checkpoint_path)
    else:
        print("Created model with fresh parameters.")
        session.run(tf.global_variables_initializer())
    return model


def train():
    """Train a shi generator model."""

    with tf.Session() as sess:
        # Create model.
        print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
        model = create_model(sess, False)

        # Read data into buckets and compute their sizes.
        print("Reading development and training data.")
        dev_set = shi_util.read_data(is_dev_set=True)
        train_set = shi_util.read_data(is_dev_set=False)

        train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
        train_total_size = float(sum(train_bucket_sizes))

        # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
        # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
        # the size if i-th training bucket, as used later.
        train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                               for i in xrange(len(train_bucket_sizes))]

        # This is the training loop.
        step_time, loss = 0.0, 0.0
        current_step = 0
        previous_losses = []

        (w2i, i2w) = shi_util.load_shi_vocab_mapping()
        while True:
            # Choose a bucket according to data distribution. We pick a random number
            # in [0, 1] and use the corresponding interval in train_buckets_scale.
            random_number_01 = np.random.random_sample()
            bucket_id = min([i for i in xrange(len(train_buckets_scale))
                             if train_buckets_scale[i] > random_number_01])

            # Get a batch and make a step.
            start_time = time.time()
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                train_set, bucket_id)
            _, step_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                         target_weights, bucket_id, False)
            step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
            loss += step_loss / FLAGS.steps_per_checkpoint
            current_step += 1

            # Once in a while, we save checkpoint, print statistics, and run evals.
            if current_step % FLAGS.steps_per_checkpoint == 0:
                # Print statistics for the previous epoch.
                perplexity = math.exp(float(loss)) if loss < 300 else float("inf")
                print("global step %d learning rate %.4f step-time %.2f perplexity "
                      "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                                step_time, perplexity))
                # Decrease learning rate if no improvement was seen over last 3 times.
                if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
                    sess.run(model.learning_rate_decay_op)
                previous_losses.append(loss)
                # Save checkpoint and zero timer and loss.
                checkpoint_path = os.path.join(FLAGS.train_dir, "shi_rnn.ckp")
                model.saver.save(sess, checkpoint_path, global_step=model.global_step)
                step_time, loss = 0.0, 0.0
                # Run evals on development set and print their perplexity.
                for bucket_id in xrange(len(_buckets)):
                    if len(dev_set[bucket_id]) == 0:
                        print("  eval: empty bucket %d" % (bucket_id))
                        continue
                    encoder_inputs, decoder_inputs, target_weights, source, target = model.get_dev_batch(
                        dev_set, bucket_id)
                    _, eval_loss, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                                 target_weights, bucket_id, True)
                    eval_ppx = math.exp(float(eval_loss)) if eval_loss < 300 else float(
                        "inf")
                    print("  eval: bucket %d perplexity %.2f" % (bucket_id, eval_ppx))

                    # TODO
                    # to randomly print out some resulting sentences
                    outputs = np.argmax(output_logits, axis=2).transpose()

                    # randomly select some examples
                    for i in xrange(_show_example_num):
                        index_to_show = randint(0, FLAGS.batch_size-1)
                        ori_text = source[index_to_show]
                        target_text = target[index_to_show]
                        out_text = outputs[index_to_show]

                        # If there is an EOS symbol in outputs, cut them at that point.
                        if data_utils.EOS_ID in ori_text:
                            ori_text = ori_text[:ori_text.index(data_utils.EOS_ID)]
                        # Print out French sentence corresponding to outputs.
                        print("Original / generated / target text: ")
                        print(" ".join([tf.compat.as_str(i2w[output]) for output in ori_text]))

                        # If there is an EOS symbol in outputs, cut them at that point.
                        if data_utils.EOS_ID in out_text:
                            out_text = out_text[:out_text.index(data_utils.EOS_ID)]
                        # Print out French sentence corresponding to outputs.
                        print(" ".join([tf.compat.as_str(i2w[output]) for output in out_text]))

                        # If there is an EOS symbol in outputs, cut them at that point.
                        if data_utils.EOS_ID in target_text:
                            target_text = target_text[:target_text.index(data_utils.EOS_ID)]
                        # Print out French sentence corresponding to outputs.
                        print(" ".join([tf.compat.as_str(i2w[output]) for output in target_text]))

                sys.stdout.flush()


def decode():
    with tf.Session() as sess:
        # Create model and load parameters.
        model = create_model(sess, True)
        model.batch_size = 1  # We decode one sentence at a time.

        # Load vocabularies.
        (w2i, i2w) = shi_util.load_shi_vocab_mapping()

        # Decode from standard input.
        sys.stdout.write("> ")
        sys.stdout.flush()
        sentence = sys.stdin.readline()
        while sentence:
            # Get token-ids for the input sentence.
            sentence = sentence.replace('\n', '')
            token_ids = shi_util.sentence_to_int_list(sentence, w2i)
            # Which bucket does it belong to?
            bucket_id = len(_buckets) - 1
            for i, bucket in enumerate(_buckets):
                if bucket[0] >= len(token_ids):
                    bucket_id = i
                    break
            else:
                logging.warning("Sentence truncated: %s", sentence)

            # Get a 1-element batch to feed the sentence to the model.
            encoder_inputs, decoder_inputs, target_weights, source, target = model.get_dev_batch(
                {bucket_id: [(token_ids, [])]}, bucket_id)
            # Get output logits for the sentence.
            _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                             target_weights, bucket_id, True)
            # This is a greedy decoder - outputs are just argmaxes of output_logits.
            # TODO implement a beam search
            # TODO is the output mapping correct? considering vocab starting from 4
            outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]

            # If there is an EOS symbol in outputs, cut them at that point.
            if data_utils.EOS_ID in outputs:
                outputs = outputs[:outputs.index(data_utils.EOS_ID)]
            # Print out French sentence corresponding to outputs.
            print(" ".join([tf.compat.as_str(i2w[output]) for output in outputs]))
            print("> ", end="")
            sys.stdout.flush()

            sentence = sys.stdin.readline()

#
# def self_test():
#     """Test the translation model."""
#     with tf.Session() as sess:
#         print("Self-test for neural translation model.")
#         # Create model with vocabularies of 10, 2 small buckets, 2 layers of 32.
#         model = seq2seq_model.Seq2SeqModel(10, 10, [(3, 3), (6, 6)], 32, 2,
#                                            5.0, 32, 0.3, 0.99, num_samples=8)
#         sess.run(tf.global_variables_initializer())
#
#         # Fake data set for both the (3, 3) and (6, 6) bucket.
#         data_set = ([([1, 1], [2, 2]), ([3, 3], [4]), ([5], [6])],
#                     [([1, 1, 1, 1, 1], [2, 2, 2, 2, 2]), ([3, 3, 3], [5, 6])])
#         for _ in xrange(5):  # Train the fake model for 5 steps.
#             bucket_id = random.choice([0, 1])
#             encoder_inputs, decoder_inputs, target_weights = model.get_batch(
#                 data_set, bucket_id)
#             model.step(sess, encoder_inputs, decoder_inputs, target_weights,
#                        bucket_id, False)


# def main(_):
#     if FLAGS.self_test:
#         self_test()
#     elif FLAGS.decode:
#         decode()
#     else:
#         train()
#
#
# if __name__ == "__main__":
#     tf.app.run()

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
"""Optimizer ops for use in layers and tf.learn."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import six

from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import clip_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import logging_ops
from tensorflow.python.ops import random_ops
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops import variables as vars_
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.training import optimizer as optimizer_
from tensorflow.python.training import training as train

OPTIMIZER_CLS_NAMES = {
    "Adagrad": train.AdagradOptimizer,
    "Adam": train.AdamOptimizer,
    "Ftrl": train.FtrlOptimizer,
    "Momentum": train.MomentumOptimizer,
    "RMSProp": train.RMSPropOptimizer,
    "SGD": train.GradientDescentOptimizer,
}

OPTIMIZER_SUMMARIES = [
    "learning_rate",
    "loss",
    "gradients",
    "gradient_norm",
]


def optimize_loss(loss,
                  global_step,
                  learning_rate,
                  optimizer,
                  gradient_noise_scale=None,
                  gradient_multipliers=None,
                  clip_gradients=None,
                  moving_average_decay=None,
                  learning_rate_decay_fn=None,
                  update_ops=None,
                  variables=None,
                  name=None,
                  summaries=None):
  """Given loss and parameters for optimizer, returns a training op.

  Args:
    loss: Tensor, 0 dimensional.
    global_step: Tensor, step counter for each update.
    learning_rate: float or Tensor, magnitude of update per each training step.
    optimizer: string, class or optimizer instance, used as trainer.
               string should be name of optimizer, like 'SGD',
                 'Adam', 'Adagrad'. Full list in OPTIMIZER_CLS_NAMES constant.
               class should be sub-class of tf.Optimizer that implements
                 `compute_gradients` and `apply_gradients` functions.
               optimizer instance should be instantion of tf.Optimizer sub-class
                 and have `compute_gradients` and `apply_gradients` functions.
    gradient_noise_scale: float or None, adds 0-mean normal noise scaled by this
                          value.
    gradient_multipliers: dict of variables or variable names to floats.
                          If present, gradients for specified
                          variables will be multiplied by given constant.
    clip_gradients: float or `None`, clips gradients by this value.
    moving_average_decay: Deprecated. float or None, takes into account previous
                          loss to make learning smoother due to outliers.
    learning_rate_decay_fn: function, takes `learning_rate` and `global_step`
                            `Tensor`s, returns `Tensor`.
                            Can be used to implement any learning rate decay
                            functions.
                            For example: tf.train.exponential_decay.
    update_ops: list of update `Operation`s to execute at each step. If `None`,
                uses elements of UPDATE_OPS collection.
    variables: list of variables to optimize or
               `None` to use all trainable variables.
    name: The name for this operation is used to scope operations and summaries.
    summaries: List of internal quantities to visualize on tensorboard. If not
               set only the loss and the learning rate will be reported. The
               complete list is in OPTIMIZER_SUMMARIES.

  Returns:
    Training op.

  Raises:
    ValueError: if optimizer is wrong type.
  """
  with vs.variable_scope(name, "OptimizeLoss", [loss, global_step]):
    # Update ops take UPDATE_OPS collection if not provided.
    if update_ops is None:
      update_ops = set(ops.get_collection(ops.GraphKeys.UPDATE_OPS))
    # Make sure update ops are ran before computing loss.
    if update_ops:
      with ops.control_dependencies(update_ops):
        barrier = control_flow_ops.no_op(name="update_barrier")
      loss = control_flow_ops.with_dependencies([barrier], loss)

    # Moving average of the loss with decay.
    # TODO(b/30439864): moving_average_decay should be removed.
    if moving_average_decay is not None:
      logging.warn("'moving_average_decay' is deprecated. Please use "
                   "tensorboard's builtin averaging instead.")
      # Generate moving averages of the loss.
      loss_averages = train.ExponentialMovingAverage(moving_average_decay,
                                                     name="avg")
      loss_averages_op = loss_averages.apply([loss])
      logging_ops.scalar_summary("loss/mean", loss_averages.average(loss))
      loss = control_flow_ops.with_dependencies([loss_averages_op], loss)

    # Learning rate variable, with possible decay.
    if (isinstance(learning_rate, ops.Tensor)
        and learning_rate.get_shape().ndims == 0):
      lr = learning_rate
    elif isinstance(learning_rate, float):
      lr = vs.get_variable(
          "learning_rate", [], trainable=False,
          initializer=init_ops.constant_initializer(learning_rate))
    else:
      raise ValueError("Learning rate should be 0d Tensor or float. "
                       "Got %s of type %s" % (
                           str(learning_rate), str(type(learning_rate))))
    if summaries is None:
      summaries = ["loss", "learning_rate"]
    if learning_rate_decay_fn is not None:
      lr = learning_rate_decay_fn(lr, global_step)
      if "learning_rate" in summaries:
        logging_ops.scalar_summary("learning_rate", lr)

    # Create optimizer, given specified parameters.
    if isinstance(optimizer, six.string_types):
      if optimizer not in OPTIMIZER_CLS_NAMES:
        raise ValueError(
            "Optimizer name should be one of [%s], you provided %s."
            % (", ".join(OPTIMIZER_CLS_NAMES), optimizer))
      opt = OPTIMIZER_CLS_NAMES[optimizer](learning_rate=lr)
    elif isinstance(optimizer, type) and issubclass(optimizer,
                                                    optimizer_.Optimizer):
      opt = optimizer(learning_rate=lr)
    elif isinstance(optimizer, optimizer_.Optimizer):
      opt = optimizer
    else:
      raise ValueError("Unrecognized optimizer: should be string, "
                       "subclass of Optimizer or instance of "
                       "subclass of Optimizer. Got %s." % str(optimizer))

    # All trainable variables, if specific variables are not specified.
    if variables is None:
      variables = vars_.trainable_variables()

    # Compute gradients.
    gradients = opt.compute_gradients(loss, variables)

    # Optionally add gradient noise.
    if gradient_noise_scale is not None:
      gradients = _add_scaled_noise_to_gradients(
          gradients, gradient_noise_scale)

    # Multiply some gradients.
    if gradient_multipliers is not None:
      gradients = _multiply_gradients(gradients, gradient_multipliers)

    # Optionally clip gradients by global norm.
    if clip_gradients is not None:
      gradients = _clip_gradients_by_norm(gradients, clip_gradients)

    # Add scalar summary for loss.
    if "loss" in summaries:
      logging_ops.scalar_summary("loss", loss)

    # Add histograms for variables, gradients and gradient norms.
    for gradient, variable in gradients:
      if isinstance(gradient, ops.IndexedSlices):
        grad_values = gradient.values
      else:
        grad_values = gradient

      if grad_values is not None:
        if "gradients" in summaries:
          logging_ops.histogram_summary(variable.name + "/gradients",
                                        grad_values)
        if "gradient_norm" in summaries:
          logging_ops.histogram_summary(variable.name + "/gradient_norm",
                                        clip_ops.global_norm([grad_values]))

    # Create gradient updates.
    grad_updates = opt.apply_gradients(gradients,
                                       global_step=global_step,
                                       name="train")
    # Make sure total_loss is valid.
    final_loss = array_ops.check_numerics(loss, "Loss is inf or nan")

    # Ensure the train_tensor computes grad_updates.
    train_tensor = control_flow_ops.with_dependencies(
        [grad_updates], final_loss)

    return train_tensor


def _clip_gradients_by_norm(grads_and_vars, clip_gradients):
  """Clips gradients by global norm."""
  gradients, variables = zip(*grads_and_vars)
  clipped_gradients, _ = clip_ops.clip_by_global_norm(gradients,
                                                      clip_gradients)
  return list(zip(clipped_gradients, variables))


def _add_scaled_noise_to_gradients(grads_and_vars, gradient_noise_scale):
  """Adds scaled noise from a 0-mean normal distribution to gradients."""
  gradients, variables = zip(*grads_and_vars)
  noisy_gradients = []
  for gradient in gradients:
    if gradient is None:
      noisy_gradients.append(None)
      continue
    if isinstance(gradient, ops.IndexedSlices):
      gradient_shape = gradient.dense_shape
    else:
      gradient_shape = gradient.get_shape()
    noise = random_ops.truncated_normal(gradient_shape) * gradient_noise_scale
    noisy_gradients.append(gradient + noise)
  return list(zip(noisy_gradients, variables))


def _multiply_gradients(grads_and_vars, gradient_multipliers):
  """Multiply specified gradients."""
  multiplied_grads_and_vars = []
  for grad, var in grads_and_vars:
    if (grad is not None and
        (var in gradient_multipliers or var.name in gradient_multipliers)):
      key = var if var in gradient_multipliers else var.name
      multiplier = constant_op.constant(
          gradient_multipliers[key], dtype=dtypes.float32)
      if isinstance(grad, ops.IndexedSlices):
        grad_values = grad.values * multiplier
        grad = ops.IndexedSlices(grad_values, grad.indices, grad.dense_shape)
      else:
        grad *= multiplier
    multiplied_grads_and_vars.append((grad, var))
  return multiplied_grads_and_vars

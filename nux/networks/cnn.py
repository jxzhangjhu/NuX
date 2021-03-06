import jax.numpy as jnp
from jax import jit, random
from functools import partial
import jax
import haiku as hk
import nux.util as util
from typing import Optional, Mapping, Callable, Sequence, Any
# import nux.weight_initializers
import nux.util.weight_initializers as init
import warnings

__all__ = ["Conv",
           "BottleneckConv",
           "ReverseBottleneckConv",
           "CNN"]

################################################################################################################

def data_dependent_param_init(x: jnp.ndarray,
                              kernel_shape: Sequence[int],
                              out_channel: int,
                              name_suffix: str="",
                              w_init: Callable=None,
                              b_init: Callable=None,
                              parameter_norm: str=None,
                              use_bias: bool=True,
                              is_training: bool=True,
                              **conv_kwargs):
  batch_size, H, W, C = x.shape
  w_shape = kernel_shape + (C, out_channel)

  if parameter_norm == "spectral_norm":
    return init.conv_weight_with_spectral_norm(x=x,
                                               kernel_shape=kernel_shape,
                                               out_channel=out_channel,
                                               name_suffix=name_suffix,
                                               w_init=w_init,
                                               b_init=b_init,
                                               use_bias=use_bias,
                                               is_training=is_training,
                                               **conv_kwargs)

  elif parameter_norm == "weight_norm":
    if x.shape[0] > 1:
      return init.conv_weight_with_weight_norm(x,
                                               kernel_shape,
                                               out_channel,
                                               name_suffix,
                                               w_init,
                                               b_init,
                                               use_bias,
                                               is_training,
                                               **conv_kwargs)
    else:
      warnings.warn("Not using weight normalization!")


  w = hk.get_parameter(f"w_{name_suffix}", w_shape, x.dtype, init=w_init)
  if use_bias:
    b = hk.get_parameter(f"b_{name_suffix}", (out_channel,), x.dtype, init=b_init)

  if use_bias:
    return w, b
  return w

################################################################################################################

class Conv(hk.Module):

  def __init__(self,
               out_channel: int,
               kernel_shape: Sequence[int],
               parameter_norm: str=None,
               stride: Optional[Sequence[int]]=(1, 1),
               padding: str="SAME",
               lhs_dilation: Sequence[int]=(1, 1),
               rhs_dilation: Sequence[int]=(1, 1),
               w_init: Callable=None,
               b_init: Callable=None,
               use_bias: bool=True,
               transpose: bool=False,
               zero_init: bool=False,
               name=None):
    super().__init__(name=name)
    self.out_channel = out_channel

    self.parameter_norm = parameter_norm

    self.kernel_shape = kernel_shape
    self.padding      = padding
    self.stride       = stride

    if True or zero_init:
      self.w_init = hk.initializers.RandomNormal(stddev=0.01)
    else:
      self.w_init = hk.initializers.VarianceScaling(1.0, "fan_avg", "truncated_normal") if w_init is None else w_init
    self.b_init = jnp.zeros if b_init is None else b_init

    self.use_bias = use_bias

    self.lhs_dilation      = lhs_dilation
    self.rhs_dilation      = rhs_dilation
    self.dimension_numbers = ('NHWC', 'HWIO', 'NHWC')

    self.transpose = transpose

    self.conv_kwargs = dict(stride=self.stride,
                            padding=self.padding,
                            lhs_dilation=self.lhs_dilation,
                            rhs_dilation=self.rhs_dilation,
                            dimension_numbers=self.dimension_numbers,
                            transpose=self.transpose)

  def __call__(self, x, is_training=True, **kwargs):
    # This function assumes that the input is batched!
    batch_size, H, W, C = x.shape

    params = data_dependent_param_init(x,
                                       self.kernel_shape,
                                       self.out_channel,
                                       name_suffix="",
                                       w_init=self.w_init,
                                       b_init=self.b_init,
                                       parameter_norm=self.parameter_norm,
                                       use_bias=self.use_bias,
                                       is_training=is_training,
                                       **self.conv_kwargs)
    if self.use_bias:
      w, b = params
    else:
      w = params

    out = util.apply_conv(x, w, **self.conv_kwargs)

    if self.use_bias:
      out += b

    return out

################################################################################################################

class RepeatedConv(hk.Module):

  def __init__(self,
               channel_sizes: Sequence[int],
               kernel_shapes: Sequence[Sequence[int]],
               parameter_norm: str=None,
               stride: Optional[Sequence[int]]=(1, 1),
               padding: str="SAME",
               lhs_dilation: Sequence[int]=(1, 1),
               rhs_dilation: Sequence[int]=(1, 1),
               w_init: Callable=None,
               b_init: Callable=None,
               use_bias: bool=False,
               normalization: str=None,
               nonlinearity: str="relu",
               dropout_rate: Optional[float]=None,
               gate: bool=True,
               name=None):
    super().__init__(name=name)

    assert len(channel_sizes) == len(kernel_shapes)
    self.channel_sizes = channel_sizes
    self.kernel_shapes = kernel_shapes
    self.dropout_rate  = dropout_rate
    self.gate          = gate

    self.conv_kwargs = dict(parameter_norm=parameter_norm,
                            stride=stride,
                            padding=padding,
                            lhs_dilation=lhs_dilation,
                            rhs_dilation=rhs_dilation,
                            w_init=w_init,
                            b_init=b_init,
                            use_bias=use_bias,
                            transpose=False)

    if nonlinearity == "relu":
      self.nonlinearity = jax.nn.relu
    elif nonlinearity == "tanh":
      self.nonlinearity = jnp.tanh
    elif nonlinearity == "sigmoid":
      self.nonlinearity = jax.nn.sigmoid
    elif nonlinearity == "swish":
      self.nonlinearity = jax.nn.swish
    elif nonlinearity == "lipswish":
      self.nonlinearity = lambda x: jax.nn.swish(x)/1.1
    else:
      assert 0, "Invalid nonlinearity"

    if normalization == "batch_norm":
      self.norm = lambda name: hk.BatchNorm(name=name, create_scale=True, create_offset=True, decay_rate=0.9, data_format="channels_last")

    elif normalization == "instance_norm":
      def norm(name):
        instance_norm = hk.InstanceNorm(name=name, create_scale=True, create_offset=True)
        def norm_apply(x, **kwargs): # So that this code works with the is_training kwarg
          return instance_norm(x)
        return norm_apply
      self.norm = norm

    elif normalization == "layer_norm":
      def norm(name):
        instance_norm = hk.LayerNorm(axis=-1, name=name, create_scale=True, create_offset=True)
        def norm_apply(x, **kwargs): # So that this code works with the is_training kwarg
          return instance_norm(x)
        return norm_apply
      self.norm = norm

    else:
      self.norm = None

  def __call__(self, x, rng, is_training=True, **kwargs):
    # This function assumes that the input is batched!
    batch_size, H, W, C = x.shape

    if rng.ndim > 1:
      # In case we did the split in ResNet or CNN
      assert rng.ndim == 2
      assert rng.shape[0] == len(self.channel_sizes)
      rngs = rng
    else:
      rngs = random.split(rng, len(self.channel_sizes))

    for i, (rng, out_channel, kernel_shape) in enumerate(zip(rngs, self.channel_sizes, self.kernel_shapes)):

      if i == len(self.channel_sizes) - 1 and self.gate == True:
        ab = Conv(2*out_channel, kernel_shape, name=f"conv_{i}", **self.conv_kwargs)(x, is_training=is_training)
        a, b = jnp.split(ab, 2, axis=-1)
        x = a*jax.nn.sigmoid(b)
      else:
        x = Conv(out_channel, kernel_shape, name=f"conv_{i}", **self.conv_kwargs)(x, is_training=is_training)

      if self.norm is not None:
        x = self.norm(f"norm_{i}")(x, is_training=is_training)

      if i < len(self.channel_sizes) - 1:
        x = self.nonlinearity(x)

        if self.dropout_rate is not None:
          rate = self.dropout_rate if is_training else 0.0
          x = hk.dropout(rng, rate, x)

    return x

################################################################################################################

class BottleneckConv(RepeatedConv):
  """ Use if we have a big input channel """
  def __init__(self,
               hidden_channel: int,
               out_channel: int,
               parameter_norm: str=None,
               normalization: str=None,
               nonlinearity: str="relu",
               dropout_rate: Optional[float]=None,
               gate: bool=True,
               use_bias: bool=False,
               name=None):

    channel_sizes = [hidden_channel, hidden_channel, out_channel]
    kernel_shapes = [(1, 1), (3, 3), (1, 1)]

    super().__init__(channel_sizes=channel_sizes,
                     kernel_shapes=kernel_shapes,
                     parameter_norm=parameter_norm,
                     normalization=normalization,
                     nonlinearity=nonlinearity,
                     use_bias=use_bias,
                     dropout_rate=dropout_rate,
                     gate=gate,
                     name=name)

class ReverseBottleneckConv(RepeatedConv):
  """ Use if we have a small input channel """
  def __init__(self,
               hidden_channel: int,
               out_channel: int,
               parameter_norm: str=None,
               normalization: str=None,
               nonlinearity: str="relu",
               dropout_rate: Optional[float]=None,
               gate: bool=True,
               use_bias: bool=False,
               name=None):

    channel_sizes = [hidden_channel, hidden_channel, out_channel]
    kernel_shapes = [(3, 3), (1, 1), (3, 3)]

    super().__init__(channel_sizes=channel_sizes,
                     kernel_shapes=kernel_shapes,
                     parameter_norm=parameter_norm,
                     normalization=normalization,
                     nonlinearity=nonlinearity,
                     use_bias=use_bias,
                     dropout_rate=dropout_rate,
                     gate=gate,
                     name=name)

################################################################################################################

class CNN(hk.Module):

  def __init__(self,
               n_blocks: int,
               hidden_channel: int,
               out_channel: int,
               working_channel: int=None,
               parameter_norm: str=None,
               normalization: str=None,
               nonlinearity: str="relu",
               squeeze_excite: bool=False,
               block_type: str="reverse_bottleneck",
               zero_init: bool=False,
               dropout_rate: Optional[float]=0.2,
               name=None):
    super().__init__(name=name)

    self.conv_block_kwargs = dict(hidden_channel=hidden_channel,
                                  parameter_norm=parameter_norm,
                                  normalization=normalization,
                                  nonlinearity=nonlinearity,
                                  dropout_rate=dropout_rate)

    self.hidden_channel = hidden_channel
    self.n_blocks       = n_blocks
    self.out_channel    = out_channel
    self.squeeze_excite = squeeze_excite
    self.zero_init      = zero_init

    if working_channel is None:
      self.working_channel = hidden_channel

    if block_type == "bottleneck":
      self.conv_block = BottleneckConv
    elif block_type == "reverse_bottleneck":
      self.conv_block = ReverseBottleneckConv
    else:
      assert 0, "Invalid block type"

  def __call__(self, x, rng, is_training=True, **kwargs):
    rngs = random.split(rng, len(self.n_blocks))

    for i, rng in enumerate(rngs):
      x = self.conv_block(out_channel=self.hidden_channel,
                          **self.conv_block_kwargs)(x, rng, is_training=is_training)

      if self.squeeze_excite:
        x = nux.SqueezeExcitation(reduce_ratio=4)(x)

    # Add an extra convolution to change the out channels
    conv = Conv(self.out_channel,
                kernel_shape=(1, 1),
                stride=(1, 1),
                padding="SAME",
                parameter_norm=self.conv_block_kwargs["parameter_norm"],
                use_bias=False,
                zero_init=self.zero_init)
    x = conv(x, is_training=is_training)

    return x

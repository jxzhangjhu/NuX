import jax
import jax.numpy as jnp
from jax import random, vmap, jit
from functools import partial
from jax.flatten_util import ravel_pytree

""" Taken from https://github.com/google/jax/blob/4a20eea8285d6396b50451ed884c0fe00e382821/docs/notebooks/Custom_derivative_rules_for_Python_code.ipynb
    and refactored to match http://www.autodiff.org/Docs/euroad/Second%20EuroAd%20Workshop%20-%20Sebastian%20Schlenkrich%20-%20Differentianting%20Fixed%20Point%20Iterations%20with%20ADOL-C.pdf"""

__all__ = ["fixed_point"]

@partial(jit, static_argnums=(0, 2))
def _fixed_point(f, x_init, max_iters):
  atol = 1e-5

  def cond_fun(val):
    x_prev, x, i = val
    max_iters_reached = jnp.where(i >= max_iters, True, False)

    flat_x_prev = ravel_pytree(x_prev)[0]
    flat_x = ravel_pytree(x)[0]
    tolerance_achieved = jnp.allclose(flat_x_prev, flat_x, atol=atol)
    return ~(max_iters_reached | tolerance_achieved)

  def body_fun(val):
    _, x, i = val
    fx = f(x)
    return x, fx, i + 1

  _, x, N = jax.lax.while_loop(cond_fun, body_fun, (x_init, f(x_init), 0.0))
  return x, N

@partial(jax.custom_vjp, nondiff_argnums=(0,))
def fixed_point(f, u, x_init, max_iters, *nondiff_args):

  def fixed_point_iter(x):
    return f(u, x, *nondiff_args)

  x, N = _fixed_point(fixed_point_iter, x_init, max_iters)
  return x

def fixed_point_fwd(f, u, x_init, max_iters, *nondiff_args):
  x = fixed_point(f, u, x_init, max_iters, *nondiff_args)
  return x, (u, x, max_iters, *nondiff_args)

# # Use this if we want second derivatives.
# def fixed_point_rev(f, ctx, dLdx):
#   u, x, max_iters, *nondiff_args = ctx

#   def rev_iter(f, packed, zeta):
#     ctx, dLdx = packed
#     u, x, max_iters, *nondiff_args = ctx

#     _, vjp_x = jax.vjp(lambda x: f(u, x, *nondiff_args), x)
#     zetaT_dFdx, = vjp_x(zeta)
#     return jax.tree_multimap(lambda x, y: x + y, dLdx, zetaT_dFdx)

#   packed = (ctx, dLdx)
#   zeta = fixed_point(partial(rev_iter, f), packed, dLdx, max_iters)

#   _, vjp_u = jax.vjp(lambda u: f(u, x, *nondiff_args), u)
#   dLdu, = vjp_u(zeta)

#   if len(nondiff_args) == 0:
#     return dLdu, None, None

#   return dLdu, None, None, (None,)*len(nondiff_args)

def fixed_point_rev(f, ctx, dLdx):
  u, x, max_iters, *nondiff_args = ctx

  _, vjp_x = jax.vjp(lambda x: f(u, x, *nondiff_args), x)

  def rev_iter(zeta):
    zetaT_dFdx, = vjp_x(zeta)
    return jax.tree_multimap(lambda x, y: x + y, dLdx, zetaT_dFdx)

  zeta, N = _fixed_point(rev_iter, dLdx, max_iters)

  _, vjp_u = jax.vjp(lambda u: f(u, x, *nondiff_args), u)
  dLdu, = vjp_u(zeta)

  if len(nondiff_args) == 0:
    return dLdu, None, None

  return dLdu, None, None, (None,)*len(nondiff_args)

fixed_point.defvjp(fixed_point_fwd, fixed_point_rev)

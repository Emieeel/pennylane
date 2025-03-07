# Copyright 2018-2021 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module contains functions for adding the Autograd interface
to a PennyLane Device class.
"""
# pylint: disable=too-many-arguments
import autograd
from autograd.numpy.numpy_boxes import ArrayBox

import pennylane as qml
from pennylane import numpy as np
from pennylane.measurements import CountsMP
from pennylane.transforms import convert_to_numpy_parameters


def _execute_legacy(
    tapes, device, execute_fn, gradient_fn, gradient_kwargs, _n=1, max_diff=2, mode=None
):
    """Execute a batch of tapes with Autograd parameters on a device.

    Args:
        tapes (Sequence[.QuantumTape]): batch of tapes to execute
        device (pennylane.Device): Device to use to execute the batch of tapes.
            If the device does not provide a ``batch_execute`` method,
            by default the tapes will be executed in serial.
        execute_fn (callable): The execution function used to execute the tapes
            during the forward pass. This function must return a tuple ``(results, jacobians)``.
            If ``jacobians`` is an empty list, then ``gradient_fn`` is used to
            compute the gradients during the backwards pass.
        gradient_kwargs (dict): dictionary of keyword arguments to pass when
            determining the gradients of tapes
        gradient_fn (callable): the gradient function to use to compute quantum gradients
        _n (int): a positive integer used to track nesting of derivatives, for example
            if the nth-order derivative is requested.
        max_diff (int): If ``gradient_fn`` is a gradient transform, this option specifies
            the maximum order of derivatives to support. Increasing this value allows
            for higher order derivatives to be extracted, at the cost of additional
            (classical) computational overhead during the backwards pass.
        mode (str): Whether the gradients should be computed on the forward
            pass (``forward``) or the backward pass (``backward``).

    Returns:
        list[list[float]]: A nested list of tape results. Each element in
        the returned list corresponds in order to the provided tapes.
    """
    # pylint: disable=unused-argument
    for tape in tapes:
        # set the trainable parameters
        params = tape.get_parameters(trainable_only=False)
        tape.trainable_params = qml.math.get_trainable_indices(params)

    # pylint misidentifies autograd.builtins as a dict
    # pylint: disable=no-member
    parameters = autograd.builtins.tuple(
        [autograd.builtins.list(t.get_parameters()) for t in tapes]
    )

    return __execute_legacy(
        parameters,
        tapes=tapes,
        device=device,
        execute_fn=execute_fn,
        gradient_fn=gradient_fn,
        gradient_kwargs=gradient_kwargs,
        _n=_n,
        max_diff=max_diff,
    )[0]


@autograd.extend.primitive
def __execute_legacy(
    parameters,
    tapes=None,
    device=None,
    execute_fn=None,
    gradient_fn=None,
    gradient_kwargs=None,
    _n=1,
    max_diff=2,
):  # pylint: disable=dangerous-default-value,unused-argument
    """Autodifferentiable wrapper around ``Device.batch_execute``.

    The signature of this function is designed to work around Autograd restrictions.
    Note that the ``parameters`` argument is dependent on the ``tapes`` argument;
    this function should always be called as follows:

    >>> parameters = [autograd.builtins.list(t.get_parameters()) for t in tapes])
    >>> parameters = autograd.builtins.tuple(parameters)
    >>> _execute(parameters, tapes=tapes, device=device)

    In particular:

    - ``parameters`` is dependent on the provided tapes: always extract them as above
    - ``tapes`` is a *required* argument
    - ``device`` is a *required* argument

    The private argument ``_n`` is used to track nesting of derivatives, for example
    if the nth-order derivative is requested. Do not set this argument unless you
    understand the consequences!
    """
    unwrapped_tapes = tuple(convert_to_numpy_parameters(t) for t in tapes)
    res, jacs = execute_fn(unwrapped_tapes, **gradient_kwargs)

    for i, r in enumerate(res):
        if any(isinstance(m, CountsMP) for m in tapes[i].measurements):
            continue

        if isinstance(r, np.ndarray):
            # For backwards compatibility, we flatten ragged tape outputs
            # when there is no sampling
            r = np.hstack(r) if r.dtype == np.dtype("object") else r
            res[i] = np.tensor(r)

        elif isinstance(res[i], tuple):
            res[i] = tuple(np.tensor(r) for r in res[i])

        else:
            res[i] = qml.math.toarray(res[i])

    return res, jacs


def _vjp_legacy(
    ans,
    parameters,
    tapes=None,
    device=None,
    execute_fn=None,
    gradient_fn=None,
    gradient_kwargs=None,
    _n=1,
    max_diff=2,
):  # pylint: disable=dangerous-default-value,unused-argument
    """Returns the vector-Jacobian product operator for a batch of quantum tapes.

    Args:
        ans (array): the result of the batch tape execution
        parameters (list[list[Any]]): Nested list of the quantum tape parameters.
            This argument should be generated from the provided list of tapes.
        tapes (Sequence[.QuantumTape]): batch of tapes to execute
        device (pennylane.Device): Device to use to execute the batch of tapes.
            If the device does not provide a ``batch_execute`` method,
            by default the tapes will be executed in serial.
        execute_fn (callable): The execution function used to execute the tapes
            during the forward pass. This function must return a tuple ``(results, jacobians)``.
            If ``jacobians`` is an empty list, then ``gradient_fn`` is used to
            compute the gradients during the backwards pass.
        gradient_fn (callable): the gradient function to use to compute quantum gradients
        gradient_kwargs (dict): dictionary of keyword arguments to pass when
            determining the gradients of tapes
        _n (int): a positive integer used to track nesting of derivatives, for example
            if the nth-order derivative is requested.
        max_diff (int): If ``gradient_fn`` is a gradient transform, this option specifies
            the maximum number of derivatives to support. Increasing this value allows
            for higher order derivatives to be extracted, at the cost of additional
            (classical) computational overhead during the backwards pass.

    Returns:
        function: this function accepts the backpropagation
        gradient output vector, and computes the vector-Jacobian product
    """
    cached_jac = {}

    def _get_jac_with_caching():
        if "jacobian" in cached_jac:
            return cached_jac["jacobian"]

        jacs = []
        for t in tapes:
            g_tapes, fn = gradient_fn(t, **gradient_kwargs)

            unwrapped_tapes = tuple(convert_to_numpy_parameters(g_t) for g_t in g_tapes)
            res, _ = execute_fn(unwrapped_tapes, **gradient_kwargs)
            jacs.append(fn(res))

        cached_jac["jacobian"] = jacs
        return jacs

    def grad_fn(dy):
        """Returns the vector-Jacobian product with given
        parameter values and output gradient dy"""

        dy = [qml.math.T(d) for d in dy[0]]

        computing_jacobian = _n == max_diff
        if gradient_fn and gradient_fn.__name__ == "param_shift" and computing_jacobian:
            jacs = _get_jac_with_caching()
        else:
            jacs = ans[1]

        if jacs:
            # Jacobians were computed on the forward pass (mode="forward")
            # No additional quantum evaluations needed; simply compute the VJPs directly.
            vjps = [qml.gradients.compute_vjp(d, jac) for d, jac in zip(dy, jacs)]

        else:
            # Need to compute the Jacobians on the backward pass (accumulation="backward")

            if isinstance(gradient_fn, qml.gradients.gradient_transform):
                # Gradient function is a gradient transform.

                # Generate and execute the required gradient tapes
                if _n == max_diff:
                    unwrapped_tapes = tuple(convert_to_numpy_parameters(t) for t in tapes)
                    vjp_tapes, processing_fn = qml.gradients.batch_vjp(
                        unwrapped_tapes,
                        dy,
                        gradient_fn,
                        reduction="append",
                        gradient_kwargs=gradient_kwargs,
                    )

                    vjps = processing_fn(execute_fn(vjp_tapes)[0])

                else:
                    vjp_tapes, processing_fn = qml.gradients.batch_vjp(
                        tapes, dy, gradient_fn, reduction="append", gradient_kwargs=gradient_kwargs
                    )
                    # This is where the magic happens. Note that we call ``execute``.
                    # This recursion, coupled with the fact that the gradient transforms
                    # are differentiable, allows for arbitrary order differentiation.
                    vjps = processing_fn(
                        _execute_legacy(
                            vjp_tapes,
                            device,
                            execute_fn,
                            gradient_fn,
                            gradient_kwargs,
                            _n=_n + 1,
                            max_diff=max_diff,
                        )
                    )
            else:
                # Gradient function is not a gradient transform
                # (e.g., it might be a device method).
                # Note that unlike the previous branch:
                #
                # - there is no recursion here
                # - gradient_fn is not differentiable
                #
                # so we cannot support higher-order derivatives.
                unwrapped_tapes = tuple(convert_to_numpy_parameters(t) for t in tapes)
                jacs = gradient_fn(unwrapped_tapes, **gradient_kwargs)

                vjps = [qml.gradients.compute_vjp(d, jac) for d, jac in zip(dy, jacs)]
        return_vjps = [
            qml.math.to_numpy(v, max_depth=_n) if isinstance(v, ArrayBox) else v for v in vjps
        ]
        return return_vjps

    return grad_fn


autograd.extend.defvjp(__execute_legacy, _vjp_legacy, argnums=[0])


#################


def execute(tapes, device, execute_fn, gradient_fn, gradient_kwargs, _n=1, max_diff=2):
    """Execute a batch of tapes with Autograd parameters on a device.

    Args:
        tapes (Sequence[.QuantumTape]): batch of tapes to execute
        device (pennylane.Device): Device to use to execute the batch of tapes.
            If the device does not provide a ``batch_execute`` method,
            by default the tapes will be executed in serial.
        execute_fn (callable): The execution function used to execute the tapes
            during the forward pass. This function must return a tuple ``(results, jacobians)``.
            If ``jacobians`` is an empty list, then ``gradient_fn`` is used to
            compute the gradients during the backwards pass.
        gradient_kwargs (dict): dictionary of keyword arguments to pass when
            determining the gradients of tapes
        gradient_fn (callable): the gradient function to use to compute quantum gradients
        _n (int): a positive integer used to track nesting of derivatives, for example
            if the nth-order derivative is requested.
        max_diff (int): If ``gradient_fn`` is a gradient transform, this option specifies
            the maximum order of derivatives to support. Increasing this value allows
            for higher order derivatives to be extracted, at the cost of additional
            (classical) computational overhead during the backwards pass.

    Returns:
        list[list[float]]: A nested list of tape results. Each element in
        the returned list corresponds in order to the provided tapes.
    """
    if not qml.active_return():
        return _execute_legacy(
            tapes,
            device,
            execute_fn,
            gradient_fn,
            gradient_kwargs,
            _n=_n,
            max_diff=max_diff,
        )
    # pylint: disable=unused-argument
    for tape in tapes:
        # set the trainable parameters
        params = tape.get_parameters(trainable_only=False)
        tape.trainable_params = qml.math.get_trainable_indices(params)

    # pylint misidentifies autograd.builtins as a dict
    # pylint: disable=no-member
    parameters = autograd.builtins.tuple(
        [autograd.builtins.list(t.get_parameters()) for t in tapes]
    )
    return _execute(
        parameters,
        tapes=tapes,
        device=device,
        execute_fn=execute_fn,
        gradient_fn=gradient_fn,
        gradient_kwargs=gradient_kwargs,
        _n=_n,
        max_diff=max_diff,
    )[0]


@autograd.extend.primitive
def _execute(
    parameters,
    tapes=None,
    device=None,
    execute_fn=None,
    gradient_fn=None,
    gradient_kwargs=None,
    _n=1,
    max_diff=2,
):  # pylint: disable=dangerous-default-value,unused-argument
    """Autodifferentiable wrapper around ``Device.batch_execute``.

    The signature of this function is designed to work around Autograd restrictions.
    Note that the ``parameters`` argument is dependent on the ``tapes`` argument;
    this function should always be called as follows:

    >>> parameters = [autograd.builtins.list(t.get_parameters()) for t in tapes])
    >>> parameters = autograd.builtins.tuple(parameters)
    >>> _execute(parameters, tapes=tapes, device=device)

    In particular:

    - ``parameters`` is dependent on the provided tapes: always extract them as above
    - ``tapes`` is a *required* argument
    - ``device`` is a *required* argument

    The private argument ``_n`` is used to track nesting of derivatives, for example
    if the nth-order derivative is requested. Do not set this argument unless you
    understand the consequences!
    """
    unwrapped_tapes = tuple(convert_to_numpy_parameters(t) for t in tapes)
    res, jacs = execute_fn(unwrapped_tapes, **gradient_kwargs)

    return res, jacs


def vjp(
    ans,
    parameters,
    tapes=None,
    device=None,
    execute_fn=None,
    gradient_fn=None,
    gradient_kwargs=None,
    _n=1,
    max_diff=2,
):  # pylint: disable=dangerous-default-value,unused-argument
    """Returns the vector-Jacobian product operator for a batch of quantum tapes.

    Args:
        ans (array): the result of the batch tape execution
        parameters (list[list[Any]]): Nested list of the quantum tape parameters.
            This argument should be generated from the provided list of tapes.
        tapes (Sequence[.QuantumTape]): batch of tapes to execute
        device (pennylane.Device): Device to use to execute the batch of tapes.
            If the device does not provide a ``batch_execute`` method,
            by default the tapes will be executed in serial.
        execute_fn (callable): The execution function used to execute the tapes
            during the forward pass. This function must return a tuple ``(results, jacobians)``.
            If ``jacobians`` is an empty list, then ``gradient_fn`` is used to
            compute the gradients during the backwards pass.
        gradient_fn (callable): the gradient function to use to compute quantum gradients
        gradient_kwargs (dict): dictionary of keyword arguments to pass when
            determining the gradients of tapes
        _n (int): a positive integer used to track nesting of derivatives, for example
            if the nth-order derivative is requested.
        max_diff (int): If ``gradient_fn`` is a gradient transform, this option specifies
            the maximum number of derivatives to support. Increasing this value allows
            for higher order derivatives to be extracted, at the cost of additional
            (classical) computational overhead during the backwards pass.

    Returns:
        function: this function accepts the backpropagation
        gradient output vector, and computes the vector-Jacobian product
    """
    cached_jac = {}

    def _get_jac_with_caching():
        if "jacobian" in cached_jac:
            return cached_jac["jacobian"]

        jacs = []
        if isinstance(device, qml.devices.experimental.Device):
            shot_vector = (
                tapes[0].shots.shot_vector if tapes[0].shots.has_partitioned_shots else None
            )
        else:
            shot_vector = device.shot_vector

        def partial_gradient_fn(tape):
            return gradient_fn(tape, shots=shot_vector, **gradient_kwargs)

        g_tapes, fn = qml.transforms.map_batch_transform(partial_gradient_fn, tapes)
        unwrapped_tapes = tuple(convert_to_numpy_parameters(g_t) for g_t in g_tapes)

        res, _ = execute_fn(unwrapped_tapes, **gradient_kwargs)

        jacs = fn(res)
        cached_jac["jacobian"] = jacs
        return jacs

    def grad_fn(dy):
        """Returns the vector-Jacobian product with given
        parameter values and output gradient dy"""
        # multi measurement
        multi_measurements = [len(tape.measurements) > 1 for tape in tapes]
        dy = dy[0]

        computing_jacobian = _n == max_diff
        if isinstance(device, qml.devices.experimental.Device):  # pragma: no-cover
            # assumes all tapes have the same shot vector
            has_partitioned_shots = tapes[0].shots.has_partitioned_shots
            vjp_shots = None
        else:
            has_partitioned_shots = vjp_shots = device.shot_vector

        if gradient_fn and gradient_fn.__name__ == "param_shift" and computing_jacobian:
            jacs = _get_jac_with_caching()
        else:
            jacs = ans[1]

        if jacs:
            # Jacobians were computed on the forward pass (mode="forward") or the Jacobian was cached
            # No additional quantum evaluations needed; simply compute the VJPs directly.
            vjps = _compute_vjps_autograd(jacs, dy, multi_measurements, has_partitioned_shots)

        else:
            # Need to compute the Jacobians on the backward pass (accumulation="backward")
            if isinstance(gradient_fn, qml.gradients.gradient_transform):
                # Gradient function is a gradient transform.

                # Generate and execute the required gradient tapes
                if _n == max_diff:
                    unwrapped_tapes = tuple(convert_to_numpy_parameters(t) for t in tapes)
                    vjp_tapes, processing_fn = qml.gradients.batch_vjp(
                        unwrapped_tapes,
                        dy,
                        gradient_fn,
                        shots=vjp_shots,
                        reduction="append",
                        gradient_kwargs=gradient_kwargs,
                    )

                    vjps = processing_fn(execute_fn(vjp_tapes)[0])

                else:
                    vjp_tapes, processing_fn = qml.gradients.batch_vjp(
                        tapes,
                        dy,
                        gradient_fn,
                        shots=vjp_shots,
                        reduction="append",
                        gradient_kwargs=gradient_kwargs,
                    )

                    # This is where the magic happens. Note that we call ``execute``.
                    # This recursion, coupled with the fact that the gradient transforms
                    # are differentiable, allows for arbitrary order differentiation.
                    vjps = processing_fn(
                        execute(
                            vjp_tapes,
                            device,
                            execute_fn,
                            gradient_fn,
                            gradient_kwargs,
                            _n=_n + 1,
                            max_diff=max_diff,
                        )
                    )

            else:
                # Gradient function is not a gradient transform
                # (e.g., it might be a device method).
                # Note that unlike the previous branch:
                #
                # - there is no recursion here
                # - gradient_fn is not differentiable
                #
                # so we cannot support higher-order derivatives.
                unwrapped_tapes = tuple(convert_to_numpy_parameters(t) for t in tapes)
                jacs = gradient_fn(unwrapped_tapes, **gradient_kwargs)

                vjps = _compute_vjps_autograd(jacs, dy, multi_measurements, has_partitioned_shots)

        return_vjps = [
            qml.math.to_numpy(v, max_depth=_n) if isinstance(v, ArrayBox) else v for v in vjps
        ]

        return return_vjps

    return grad_fn


def _compute_vjps_autograd(jacs, dy, multi_measurements, has_partitioned_shots):
    """Compute the vjps of multiple tapes, directly for a Jacobian and co-tangents dys."""
    vjps = []
    for i, multi in enumerate(multi_measurements):
        dy_ = dy[i] if has_partitioned_shots else (dy[i],)
        jac_ = jacs[i] if has_partitioned_shots else (jacs[i],)

        shot_vjps = []
        for d, j in zip(dy_, jac_):
            if multi:
                shot_vjps.append(qml.gradients.compute_vjp_multi(d, j))
            else:
                shot_vjps.append(qml.gradients.compute_vjp_single(d, j))

        vjps.append(qml.math.sum(qml.math.stack(shot_vjps), axis=0))

    return vjps


autograd.extend.defvjp(_execute, vjp, argnums=[0])

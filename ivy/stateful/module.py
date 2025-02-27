"""Base class for deriving trainable modules."""

# global
import functools
import os
import abc
import copy
import dill
from typing import Optional, Tuple, Dict

# local
import ivy
from ivy.data_classes.container import Container
from ivy.func_wrapper import _get_first_array
from ivy.stateful.helpers import ModuleHelpers
from ivy.stateful.converters import ModuleConverters


# Base #
# -----#


class ModuleMeta:
    def __new__(cls, *args, **kwargs):
        # check the module of the class
        # if it's stateful, it's internal
        # we leave this untouched
        if "stateful" in cls.__module__:
            # we are not assigning it a variable
            pass
        else:
            # first check if a var is already assigned
            # this would mean it is a nested custom class
            if not hasattr(Module, "_init_var"):
                # if not , create it and add
                Module._init_var = [cls]
            else:
                Module._init_var.append(cls)
        instance = super().__new__(cls)
        return instance


class Module(ModuleHelpers, ModuleConverters, ModuleMeta):
    """Module is a base class for deriving trainable modules."""

    def __init__(
        self,
        /,
        *args,
        device=None,
        v=None,
        buffers=None,
        build_mode="on_init",
        compile_on_next_step=False,
        store_vars=True,
        stateful=None,
        arg_stateful_idxs=None,
        kwarg_stateful_idxs=None,
        fallback_to_non_compiled=False,
        with_partial_v=False,
        devices=None,
        dtype=None,
        dynamic_backend=None,
        **kwargs,
    ):
        """
        Initialize Ivy layer, which is a stateful object consisting of trainable
        variables.

        Parameters
        ----------
        device
            device on which to create the module's variables 'cuda:0', 'cuda:1', 'cpu'
            etc. (Default value = None)
        v
            Ivy container of trainable variables. Created internally by default.
        build_mode
            How the Module is built, either on initialization (now),
            explicitly by the user by calling build(), or the first
            time the __call__ method is run. Default is on initialization.
        compile_on_next_step
            Whether to compile the network on the next forward pass.
            Default is ``False``.
        store_vars
            Whether or not to store the variables created. Default is ``True``.
        stateful
            The constant id stateful items to track as part of the forward pass.
            Used when graph compiling, default is ``None``.
        arg_stateful_idxs
            The nested argument indices of stateful items to track as part of
            the forward pass.
            Used when graph compiling, default is ``None``.
        kwarg_stateful_idxs
            The nested keyword argument indices of stateful items to track as part of
            the forward pass. Used when graph compiling, default is ``None``.
        fallback_to_non_compiled
            Whether to fall back to non-compiled forward call in the case that an error
            is raised during the compiled forward pass. Default is ``True``.
        with_partial_v
            Whether to allow partial specification of variables. Default is ``False``.
        devices
            devices on which to distribute the module's variables
            'cuda:0', 'cuda:1', 'cpu' etc. (Default value = None)
        """
        valid_build_modes = ["on_init", "explicit", "on_call"]
        ivy.utils.assertions.check_elem_in_list(build_mode, valid_build_modes)
        self._dev = ivy.default(
            device,
            ivy.default(
                lambda: devices[0],
                default_val=ivy.default_device(),
                catch_exceptions=True,
            ),
        )
        self._devs = ivy.default(devices, [self._dev])
        self._build_mode = build_mode
        self._stateful = stateful
        self._arg_stateful_idxs = arg_stateful_idxs
        self._kwarg_stateful_idxs = kwarg_stateful_idxs
        self._fallback_to_non_compiled = fallback_to_non_compiled
        self._with_partial_v = with_partial_v
        self._store_vars = store_vars
        self._built = False
        self._compiled = False
        self._compiled_fn = None
        self._compile_on_next_step = compile_on_next_step
        self._v_in = v if isinstance(v, Container) or v is None else Container(v)
        self.v = v
        self.top_v = None
        self.top_mod = None
        self._track_submod_rets = False
        self._submod_depth = None
        self._submods_to_track = None
        self._track_submod_call_order = False
        self.expected_submod_rets = None
        self.submod_dict = dict()
        backend = ivy.with_backend("numpy")
        self.submod_rets = ivy.Container(alphabetical_keys=False, ivyh=backend)
        self.submod_call_order = ivy.Container(alphabetical_keys=False, ivyh=backend)
        self._sub_mods = set()
        self._dtype = dtype
        self._args = args
        self._kwargs = kwargs
        self._module_graph = None
        self._target = None
        self._lazy_compiled = False
        self._dynamic_backend = dynamic_backend
        if build_mode != "on_init":
            return
        if hasattr(Module, "_init_var"):
            if "stateful" in self.__module__:
                # we know we are operating within the
                # context of another class, and it's a
                # stateful class internally defined
                # so we freeze weight generation
                # unless `v` or `with_partial_v` is passed

                if v or with_partial_v:
                    # build only if `v` or `with_partial_v`
                    self.build(
                        *args,
                        dynamic_backend=dynamic_backend,
                        buffers=buffers,
                        **kwargs,
                    )
                # we don't want to delete the class variable now
                # since there could be other child modules
                return
            # we know this is the custom class that has triggered the
            # class var, so we do the building, and after that delete
            # the class variable, but before that we check if it's a
            # nested scenario, because if it's another custom class initialised
            # within another one, then we have to hold variable initialisation
            # here too, unless `v` or `with_partial_v`
            if len(Module._init_var) > 1 and not v and not with_partial_v:
                # hold off initialisation, delete key for this class and
                # move on
                Module._init_var.pop()
                return
            self.build(
                *args, dynamic_backend=dynamic_backend, buffers=buffers, **kwargs
            )
            if Module._init_var[-1] == self.__class__.__name__:
                # you delete it, only if this is the class that caused it's creation
                Module._init_var.pop()

            # do a final check if _init_var  becomes empty, then delete it all together
            if not Module._init_var:
                del Module._init_var

            return
        self.build(*args, dynamic_backend=dynamic_backend, buffers=buffers, **kwargs)

    # Private #
    # --------#

    def _fn_with_var_arg_wrapper(
        self, *a, fn, v_fn, keychain_mappings, orig_key_chain, **kw
    ):
        if "v" in kw.keys():
            del kw["v"]
        v = v_fn(self.v, keychain_mappings, orig_key_chain)
        return fn(*a, **kw, v=v)

    def _fn_with_var_arg(self, fn, v_fn, /, keychain_mappings, orig_key_chain):
        """
        Extract variables from `v_fn` and use it as inputs for `fn`.

        Use `v_fn` to extract the variables and use the extracted
        variables as inputs to the call function fn of the module.
        """
        _fn_with_var_arg_wrapper = functools.partial(
            self._fn_with_var_arg_wrapper,
            fn=fn,
            v_fn=v_fn,
            keychain_mappings=keychain_mappings,
            orig_key_chain=orig_key_chain,
        )
        _fn_with_var_arg_wrapper.wrapped = True
        return _fn_with_var_arg_wrapper

    def _find_variables(
        self, /, *, obj=None, _visited=None, without_initialisation=False
    ):
        """
        Find all internal variables in obj. Return empty Container if obj is None.

        Parameters
        ----------
        obj
            The submodule whose internal variables are to be returned. Default
            is None.
        _visited
            Placeholder for tracking the visited nodes, do not set this parameter.

        Returns
        -------
        ret
            The internal variables of the submodule passed in the argument.
        """
        _visited = ivy.default(_visited, {})
        vs = Container()
        if id(obj) in _visited:
            return vs
        _visited[id(obj)] = True
        # ToDo: add support for finding local variables, if/when JAX supports
        #  uniquely flagging variables
        if isinstance(obj, Module) and obj is not self:
            obj.top_v = self._top_v_fn
            obj.top_mod = self._top_mod_fn
            self._sub_mods.add(obj)

            if not obj.built_ and without_initialisation:
                return lambda: obj._build_and_return_v(
                    *obj._args, dynamic_backend=self._dynamic_backend, **obj._kwargs
                )

            return obj._build_and_return_v(
                *obj._args, dynamic_backend=obj._dynamic_backend, **obj._kwargs
            )
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                ret = self._find_variables(
                    obj=v,
                    _visited=_visited,
                    without_initialisation=without_initialisation,
                )
                if ret:
                    vs["v" + str(i)] = ret
            return vs
        elif isinstance(obj, dict):
            for k, v in obj.items():
                ret = self._find_variables(
                    obj=v,
                    _visited=_visited,
                    without_initialisation=without_initialisation,
                )
                if ret:
                    vs[k[1:] if k[0] == "_" else k] = ret
            return vs
        elif not hasattr(obj, "__dict__"):
            return vs
        for k, v in obj.__dict__.items():
            if v is not None and k[0:2] != "__":
                ret = self._find_variables(
                    obj=v,
                    _visited=_visited,
                    without_initialisation=without_initialisation,
                )
                if ret:
                    vs[k[1:] if k[0] == "_" else k] = ret
        return vs

    def _build_and_return_v(self, *args, **kwargs):
        self.build(*args, **kwargs)
        return self.v

    def _find_child_objects(self, /, *, obj=None, _visited=None):
        pass

    def _find_buffers(self):
        for obj in self.__dict__.keys():
            if isinstance(getattr(self, obj), ivy.Module):
                # simply fetch it's buffer
                if hasattr(getattr(self, obj), "buffers"):
                    self.buffers.update({obj: getattr(self, obj).buffers})

    @staticmethod
    def _extract_v(v, keychain_mappings: dict, orig_key_chain, /):
        """
        Extract the variables from the variables container v using the key
        orig_key_chain and reinstantiate the duplicate variables that were removed by
        _remove_duplicate_variables in their correct locations using keychain_mappings.

        Parameters
        ----------
        v
            The variables container
        keychain_mappings
            The keychain mappings of duplicate vatriables
        orig_key_chain
            keychain of the variables to be extracted


        Returns
        -------
        ret_cont
            container with the extracted variables.
        """
        if v.cont_has_key_chain(orig_key_chain):
            ret_cont = v.cont_at_key_chain(orig_key_chain)
        else:
            ret_cont = ivy.Container()
        for old_kc, new_kc in keychain_mappings.items():
            if orig_key_chain in old_kc:
                ret_cont = ret_cont.cont_set_at_key_chain(
                    "/".join(new_kc.split("/")[1:]), v.cont_at_key_chain(new_kc)
                )
        return ret_cont

    def _wrap_call_methods(
        self, keychain_mappings, /, *, key="", obj=None, _visited=None
    ):
        """
        Wrap the call methods of the Module object by looping over all the items within
        the module, wrapping the __call__ methods of all submodules using
        _fn_with_var_arg.

        Parameters
        ----------
        keychain_mappings
            The keychain mappings of the object
        key
            The keychain of the object obj, used for recursion.
        obj
            the object whose __call__ method is to be wrapped
        _visited
            Placeholder for tracking the visited nodes, do not set this parameter.

        Returns
        -------
        None
        """
        _visited = ivy.default(_visited, {})
        if id(obj) in _visited or not isinstance(key, str):
            return
        _visited[id(obj)] = True
        if isinstance(obj, Module) and obj is not self:
            orig_key_chain = key[1:] if key[0] == "_" else key

            obj.__call__ = self._fn_with_var_arg(
                obj.__call__, self._extract_v, keychain_mappings, orig_key_chain
            )
            return
        elif isinstance(obj, (list, tuple)):
            for i, val in enumerate(obj):
                self._wrap_call_methods(
                    keychain_mappings,
                    key=key + "/v" + str(i),
                    obj=val,
                    _visited=_visited,
                )
            return
        elif isinstance(obj, dict):
            for k, val in obj.items():
                k = (key + "/" + k) if key != "" and isinstance(k, str) else k
                self._wrap_call_methods(
                    keychain_mappings, key=k, obj=val, _visited=_visited
                )
            return
        if not hasattr(obj, "__dict__"):
            return
        for k, val in obj.__dict__.items():
            if k[0:2] == "__":
                continue
            k = (key + "/" + k) if key != "" else k
            if val is not None:
                self._wrap_call_methods(
                    keychain_mappings, key=k, obj=val, _visited=_visited
                )
        return

    @staticmethod
    def _remove_duplicate_variables(vs, created, /):
        """
        Remove duplicate variables in `vs` referring to `created`.

        Parameters
        ----------
        vs
            The container that needs to be pruned.
        created
            The container as the duplication reference.

        Returns
        -------
        vs
            The container after removing duplicate variables.
        keychain_mappings
            Dict storing those keys and ids being removed.
        """
        created_ids = created.cont_map(lambda x, kc: id(x))
        vs_ids = vs.cont_map(lambda x, kc: id(x))
        ids = dict()
        duplicate_keychains = list()
        keychain_mappings = dict()

        def unique_callback(x, kc):
            ids[x] = kc

        def found_dup_callback(x, kc):
            if ids[x] == kc:
                return
            duplicate_keychains.append(kc)
            keychain_mappings[kc] = ids[x]

        created_ids.cont_map(lambda x, kc: unique_callback(x, kc))
        vs_ids.cont_map(
            lambda x, kc: (
                unique_callback(x, kc) if x not in ids else found_dup_callback(x, kc)
            )
        )
        for dup_kc in duplicate_keychains:
            vs = vs.cont_prune_key_chain(dup_kc)
        return vs, keychain_mappings

    def _set_buffers(self, buffers):
        """
        Set the buffers of the given class instance, according to the buffers passed.

        Parameters
        ----------
        buffers
            a dictionary with variable names and corresponding values

        override
            if true, sets the variable as an attribute even if it doesn't exist
        """
        for buffer in buffers:
            if hasattr(self, buffer):
                # check if this value is another nested dictionary, if yes
                # we recurse
                if isinstance(buffers[buffer], dict):
                    getattr(self, buffer)._set_buffers(buffers=buffers[buffer])
                else:
                    setattr(self, buffer, buffers[buffer])
            else:
                if hasattr(self, "buffers"):
                    self.buffers.update({buffer: buffers[buffer]})
                else:
                    setattr(self, "buffers", {buffer: buffers[buffer]})
                setattr(self, buffer, buffers[buffer])

    # Overridable #

    # noinspection PyMethodMayBeStatic,PyUnusedLocal
    def _create_variables(self, *, device=None, dtype=None):
        """
        Create internal trainable variables, and return as arbitrary nested dict.
        Overridable.

        Parameters
        ----------
        device
            The device string, specifying the device on which to create the variables.

        Returns
        -------
        ret
            An empty set.
        """
        return {}

    def _build(self, *args, **kwargs) -> bool:
        """
        Build the internal layers and variables for this module. Overridable.

        Returns
        -------
        ret
            False or empty Container if the build only partially completed (i.e. some
            child Modules have "on_call" build mode). Alternatively, return True or a
            container of the built variables if the module is built.
        """
        return True

    # Abstract #

    @abc.abstractmethod
    def _forward(self, *args, **kwargs):
        """
        Forward pass of the layer, called after handling the optional input variables.

        Raises
        ------
        NotImplementedError
        """
        raise ivy.utils.exceptions.IvyNotImplementedException

    def _forward_with_tracking(self, *args, **kwargs):
        """
        Forward pass while optionally tracking submodule returns and call order.

        Returns
        -------
        ret
            Result of the forward pass of the layer.
        """
        if self.track_submod_call_order():
            self._add_submod_enter()
        ret = self._forward(*args, **kwargs)
        track_submod_rets = self.track_submod_rets()
        check_submod_rets = self.check_submod_rets()
        if track_submod_rets or check_submod_rets:
            self._add_submod_ret(ret)
        if check_submod_rets:
            self._check_submod_ret()
        return ret

    def _call(self, *args, v=None, buffers=None, **kwargs):
        """
        Compute forward pass of the layer, treating layer instance as callable function.

        Parameters
        ----------
        v
            Replace `v` of current layer when forwarding. Restore
            after the forward finished.

        Returns
        -------
        ret
            Result of the forward pass of the layer.
        """
        if not self._built:
            self.build(
                *args,
                **kwargs,
                from_call=True,
                dtype=_get_first_array(*args, **kwargs).dtype,
            )
        if buffers:
            buffers_orig = self.buffers.copy()
            self.buffers = {}
            self._set_buffers(buffers)
        if v is not None:
            v_orig = self.v
            self.v = (
                Container(v, **v.cont_config)
                if isinstance(v, Container)
                else Container(v)
            )
            ret = self._forward_with_tracking(*args, **kwargs)
            self.v = v_orig
            if buffers:
                self.buffers = {}
                self._set_buffers(buffers_orig)
            return ret

        elif hasattr(self.__call__, "wrapped"):
            return self.__call__(*args, **kwargs)
        return self._forward_with_tracking(*args, **kwargs)

    # Public #
    # -------#
    def __call__(
        self,
        *args,
        v=None,
        buffers=None,
        stateful=None,
        arg_stateful_idxs=None,
        kwarg_stateful_idxs=None,
        track_submod_rets=False,
        submod_depth=None,
        submods_to_track=None,
        track_submod_call_order=False,
        expected_submod_rets=None,
        **kwargs,
    ):
        """
        Forward an input through current module.

        Parameters
        ----------
        v
            If given, use this container as internal varibles temporarily.
            Default is ``None``.
        track_submod_rets
            If True, will track the returns of submodules.
        submod_depth
            The depth of tracked submodules.
        submods_to_track
            If given, will only track submodules in `submods_to_track`.
        track_submod_call_order
            If True, will track the call order of submodules.
        expected_submod_rets
            If given, will raise exception if submodule returns are
            different from expected returns.

        Returns
        -------
        ret
        """
        if self._lazy_compiled:
            # we are compiling since we want to transpile module,
            # so set the appropriate backend
            if self._target:
                ivy.set_backend(self._target)
            self.compile(args=args, kwargs=kwargs)
            if self._target:
                ivy.previous_backend()

        if self._module_graph:
            # we need `v` in kwargs, since this is a compiled call
            v = v if v else self.v
            return self._module_graph(*args, v=v, **kwargs)

        backend = ivy.with_backend("numpy")
        self.submod_rets = ivy.Container(alphabetical_keys=False, ivyh=backend)
        self.submod_call_order = ivy.Container(alphabetical_keys=False, ivyh=backend)
        self._set_submod_flags(
            track_submod_rets,
            submod_depth,
            submods_to_track,
            track_submod_call_order,
            expected_submod_rets,
        )

        # convert variables to native arrays so that they can be tracked
        v = ivy.to_native(v)
        ret = self._call(*args, v=v, buffers=buffers, **kwargs)
        self._unset_submod_flags()
        return ret

    def save_weights(self, weights_path, /):
        """
        Save the weights on the Module.

        Parameters
        ----------
        weights_path
            The hdf5 file for saving the weights.

        Returns
        -------
        None
        """
        os.makedirs("/".join(weights_path.split("/")[:-1]), exist_ok=True)
        self.v.cont_to_disk_as_hdf5(weights_path)

    def build(
        self,
        *args,
        from_call=False,
        device=None,
        dtype=None,
        dynamic_backend=None,
        buffers=None,
        **kwargs,
    ):
        """
        Build the internal layers and variables for this module.

        Parameters
        ----------
        from_call
            If True, denote that this build is triggered by calling. Otherwise,
            triggered by initializing the module. Default is ``False``.
        device
            The device we want to build module on. None for default device.
            Default is ``None``.
        dtype
            The data type for building the module. Default is ``None``.

        Returns
        -------
        ret
            True for successfully built a module.
        """
        self._dev = ivy.default(device, self._dev)
        # return False if not from_call but build_mode is on_call
        if not from_call and self._build_mode == "on_call":
            return self.v
        if dtype:
            dtype = ivy.default_dtype(dtype=dtype, as_native=True)
        else:
            dtype = ivy.default_dtype(dtype=self._dtype, as_native=True)

        # why are we adding this kwarg in user-defined build ?
        # it results in the error while doing `from_haiku_module` if haiku's forward
        # therefore leaving it commented out
        # kwargs["dtype"] = dtype

        # build local Module, and any child modules flagged with "explicit" build mode
        # this gets the child modules initialised at best, their weights
        # remain un-generated
        built = ivy.default(self._build(*args, **kwargs), True)

        # this creates weights for this Module only
        created = Container(
            self._create_variables(device=self._dev, dtype=dtype), dynamic_backend=False
        )

        # build variables based on locally built layers, if v not passed in constructor
        v_from_constructor = self._v_in

        created_n_found = Container(
            dict(
                **self._find_variables(
                    obj=self,
                    without_initialisation=(
                        True
                        if v_from_constructor and not self._with_partial_v
                        else False
                    ),
                ),
                **created,
            ),
            dynamic_backend=dynamic_backend,
        )
        if ivy.exists(v_from_constructor):
            if self._with_partial_v:
                if v_from_constructor:
                    created_n_found.cont_assert_contains_sub_structure(
                        v_from_constructor, partial=True
                    )
                self.v = created_n_found.cont_set_at_key_chains(v_from_constructor)
            else:
                created_n_found, _ = self._remove_duplicate_variables(
                    created_n_found, created
                )

                ivy.Container.cont_assert_identical_structure(
                    [created_n_found, v_from_constructor],
                    build_callable=True,
                    assert_and_assign=True,
                )

                self.v = created_n_found
        else:
            self.v = created_n_found
        # remove duplicates
        self.v, keychain_mappings = self._remove_duplicate_variables(self.v, created)
        # build any child 'on_call' layers
        if not built and from_call:
            # update child modules to share the same device
            for k, v in self.__dict__.items():
                if isinstance(v, ivy.Module):
                    v._dev = self._dev

            # build during forward pass
            self._forward(*args, **kwargs)

            # re-build variables based on additional child on-call layers, if v not
            # passed in constructor
            if not ivy.exists(v_from_constructor):
                created_n_found = Container(
                    dict(
                        **self._find_variables(obj=self),
                        **self._create_variables(device=self._dev, dtype=dtype),
                    )
                )
                self.v = created_n_found

            # remove further duplicates with self.v
            self.v, keychain_mappings = self._remove_duplicate_variables(
                self.v, created
            )

            # set built flag
            built = True

        # wrap call methods if the module is fully built
        if built:
            self._wrap_call_methods(keychain_mappings, obj=self)

        # flag built and remove local variables if specified
        self._built = bool(built)
        v_ret = self.v
        if not self._store_vars:
            # ToDo: verify variables in self.v are released once this method exits
            self.v = ivy.Container()

        # once all variables built, find and assign buffers
        if buffers:
            self._set_buffers(buffers=buffers)
            self._find_buffers()

        return v_ret if bool(v_ret) or isinstance(built, bool) else built

    def register_buffer(self, var_name, value):
        """Set the buffer at any place within the class."""
        self._set_buffers({var_name: value})

    def __repr__(self):
        return object.__repr__(self)

    # Properties #
    # -----------#

    @property
    def build_mode(self):
        return self._build_mode

    @property
    def built_(self):
        return self._built

    def show_graph(
        self,
        randomness_factor: float = 0.1,
        save_to_disk: bool = False,
        notebook: bool = False,
        with_edge_labels: bool = True,
        with_arg_labels: bool = True,
        with_output_labels: bool = True,
        output_connected_only: bool = True,
        highlight_subgraph: Optional[int] = None,
        fname: Optional[str] = None,
    ):
        if not ivy.exists(self._module_graph):
            raise ValueError("You must compile the module to display the graph.")

        return self._module_graph.show(
            save_to_disk=save_to_disk,
            notebook=notebook,
            with_edge_labels=with_edge_labels,
            with_arg_labels=with_arg_labels,
            with_output_labels=with_output_labels,
            output_connected_only=output_connected_only,
            randomness_factor=randomness_factor,
            highlight_subgraph=highlight_subgraph,
            fname=fname,
        )

    def __getattribute__(self, name):
        if name == "v":
            if super().__getattribute__("v") is None and not self.built_:
                self._build_and_return_v(
                    self._args, dynamic_backend=self._dynamic_backend, **self._kwargs
                )
        if name != "buffers":
            if hasattr(self, "buffers"):
                if name in self.buffers:
                    return self.buffers[name]
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        if hasattr(self, "buffers"):
            if name in self.buffers:
                self.buffers[name] = value
                return
        return super().__setattr__(name, value)

    def __delattr__(self, name):
        if hasattr(self, "buffers"):
            if name in self.buffers:
                del self.buffers[name]
        else:
            super().__delattr__(name)

    def compile(
        self,
        args: Optional[Tuple] = None,
        kwargs: Optional[Dict] = None,
        **compile_kwargs,
    ):
        """
        Compile the `ivy.Module`'s `_unified_ivy_graph` or `_call` method to the target
        backend.

        Parameters
        ----------
        args:
            arguments used to compile. Defaults to None.
        kwargs:
            keyword arguments used to compile. Defaults to None.
        compile_kwargs:
            keyword arguments passed to the compile function.
        """
        # no arguments given to compile, so delay the compilation
        if not (args or kwargs):
            self._lazy_compiled = True
            return

        # we do not need convert the args to source
        args = ivy.default(args, tuple())
        kwargs = ivy.default(kwargs, dict())

        # shallow copy the kwargs dict
        kwargs = copy.copy(kwargs)
        kwargs["v"] = self.v

        fn_to_compile = ivy.default(self._module_graph, self._call)

        self._module_graph = ivy.compile(
            fn_to_compile, **compile_kwargs, args=args, kwargs=kwargs
        )

        self._lazy_compiled = False

    def save(self, filename):
        """
        Save the module object to disk using pickle.

        Parameters
        ----------
        filename : str
            The name of the file to save the module object to.
        """
        if ivy.current_backend_str() == "paddle":
            self._convert_tensors_to_numpy()
        with open(filename, "wb") as f:
            dill.dump(self, f)
        if ivy.current_backend_str() == "paddle":
            self._convert_numpy_to_tensors()

    @staticmethod
    def load(filename):
        """
        Load a module object from disk using pickle.

        Parameters
        ----------
        filename : str
            The name of the file to load the module object from.

        Returns
        -------
        Module
            The loaded module object.
        """
        with open(filename, "rb") as f:
            loaded = dill.load(f)
        if ivy.current_backend_str() == "paddle":
            loaded._convert_numpy_to_tensors()
        return loaded

import torch
import torch.fx
import warnings
import functools

from typing import Dict

def gen_constructor_wrapper(target):
    @functools.wraps(target)
    def wrapper(*args, **kwargs):
        proxy = None
        def check_has_proxy(v):
            if isinstance(v, torch.fx.Proxy):
                nonlocal proxy
                proxy = v
        torch.fx.node.map_aggregate(args, check_has_proxy)
        torch.fx.node.map_aggregate(kwargs, check_has_proxy)

        if proxy is not None:
            return proxy.tracer.create_proxy('call_function', target, args, kwargs)
        else:
            return target(*args, **kwargs)
    return wrapper, target

class MetaProxy(torch.fx.Proxy):
    def install_tensor_meta(self, tensor_meta):
        self._tensor_meta = tensor_meta

    def size(self, dim=None):
        if hasattr(self, '_tensor_meta') and self._tensor_meta is not None:
            return self._tensor_meta.size(*[dim] if dim else [])
        return self.tracer.create_proxy('call_method', 'size', (self, dim) if dim else (self,), {})

    def dim(self):
        if hasattr(self, '_tensor_meta') and self._tensor_meta is not None:
            return self._tensor_meta.dim()
        return self.tracer.create_proxy('call_method', 'dim', (self,), {})
        
    def __getattr__(self, k):
        if k == '_tensor_meta':
            return self.__getattribute__(k)
        # note: not added to the graph yet, if this is a method call
        # we peephole optimize to the method invocation
        return MetaAttribute(self, k)

class MetaAttribute(MetaProxy):
    def __init__(self, root, attr: str):

        self.root = root
        self.attr = attr
        self.tracer = root.tracer
        self._node = None

    @property
    def node(self):
        # the node for attributes is added lazily, since most will just be method calls
        # which do not rely on the getitem call
        if self._node is None:
            self._node = self.tracer.create_proxy('call_function', getattr, (self.root, self.attr), {}).node
        return self._node

    def __call__(self, *args, **kwargs):
        return self.tracer.create_proxy('call_method', self.attr, (self.root,) + args, kwargs)

def proxys_to_metas(v):
    if isinstance(v, torch.fx.Proxy):
        assert isinstance(v, MetaProxy), f'Expected MetaProxy but got {type(v)}'
        assert hasattr(v, '_tensor_meta'), f'MetaProxy does not have an associated meta'
        return v._tensor_meta
    return v

class MetaTracer(torch.fx.Tracer):
    allow_insert_stateless_mods : bool = True

    _TORCH_METHODS_TO_PATCH = ['arange', 'zeros', 'ones', 'full_like', 'eye']

    def create_proxy(self, kind, target, args, kwargs, name=None, type_expr=None, proxy_factory_fn=None):
        rv = super().create_proxy(kind, target, args, kwargs, name, type_expr, proxy_factory_fn)

        if kind == 'placeholder' and target in self.meta_args:
            rv.install_tensor_meta(self.meta_args[target])
            return rv

        if target in self.orig_fns:
            # NOTE: tensor constructors in PyTorch define the `device` argument as
            # *kwargs-only*. That is why this works. If you add methods to
            # _TORCH_METHODS_TO_PATCH that do not define `device` as kwarg-only,
            # this will break and you will likely see issues where we cannot infer
            # the size of the output.
            if 'device' in kwargs:
                kwargs['device'] = 'meta'

        try:
            args_metas = torch.fx.node.map_aggregate(args, proxys_to_metas)
            kwargs_metas = torch.fx.node.map_aggregate(kwargs, proxys_to_metas)

            if kind == 'call_function':
                meta_out = target(*args_metas, **kwargs_metas)
            elif kind == 'call_method':
                meta_out = getattr(args_metas[0], target)(*args_metas[1:], **kwargs_metas)
            elif kind == 'call_module':
                assert hasattr(self, 'orig_forward')
                meta_out = self.orig_forward(*args_metas, **kwargs_metas)
            else:
                assert kind in ['placeholder'], f'Unsupported node kind {kind}'
                return rv

            # TODO
            assert isinstance(rv, torch.fx.Proxy), 'Dont support composite output yet'
            rv.install_tensor_meta(meta_out)
        except AssertionError as e:
            warnings.warn(f'Could not compute metadata for target {target}: {e}')

        return rv

    def call_module(self, m, forward, args, kwargs):
        self.orig_forward = forward
        return super().call_module(m, forward, args, kwargs)

    def _insert_module_as_submodule(self, mod: torch.nn.Module) -> str:
        """
        Helper method which tries to insert a module that was not declared as submodule.
        """
        idx = 0
        mod_name = mod.__class__.__name__.lower()
        path = f"{mod_name}_{idx}"
        while hasattr(self.root, path):
            path = f"{mod_name}_{idx}"
            idx += 1

        self.root.add_module(path, mod)
        return path

    def path_of_module(self, mod: torch.nn.Module) -> str:
        try:
            return super().path_of_module(mod)
        except NameError as e:
            if self.allow_insert_stateless_mods and len(list(mod.parameters())) == 0 and len(list(mod.buffers())) == 0:
                path = self._insert_module_as_submodule(mod)
                self.prev_module = path
                return path
            raise

    def proxy(self, node):
        return MetaProxy(node, self)

    def trace(self, root, meta_args : Dict[str, torch.Tensor], concrete_args=None):
        assert isinstance(meta_args, dict)
        self.meta_args = meta_args

        self.patched_torch_methods = {
            target: gen_constructor_wrapper(getattr(torch, target)) for target in self._TORCH_METHODS_TO_PATCH
        }
        self.orig_fns = set()

        for name, (wrapper, orig) in self.patched_torch_methods.items():
            setattr(torch, name, wrapper)
            self.orig_fns.add(orig)

        try:
            return super().trace(root, concrete_args)
        finally:
            for name, (_, orig) in self.patched_torch_methods.items():
                setattr(torch, name, orig)


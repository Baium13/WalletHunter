"""Bounded display/public cache. Callers retain their original TTL checks."""
from collections import OrderedDict


class BoundedCache(OrderedDict):
    def __init__(self,capacity=256):
        super().__init__();self.capacity=capacity
    def __setitem__(self,key,value):
        super().__setitem__(key,value);self.move_to_end(key)
        while len(self)>self.capacity:self.popitem(last=False)

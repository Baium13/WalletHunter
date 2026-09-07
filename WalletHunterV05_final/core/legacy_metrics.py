"""One-off migration of legacy infinite analytical PF, never trading amounts."""
import copy
import math


def normalise_legacy_metrics(document):
    result=copy.deepcopy(document)
    changed=[]
    def visit(value,path):
        if isinstance(value,dict):
            return {k:visit(v,path+[str(k)]) for k,v in value.items()}
        if isinstance(value,list):return [visit(v,path+[str(i)]) for i,v in enumerate(value)]
        if isinstance(value,float) and not math.isfinite(value):
            allowed=(len(path)==5 and path[0]=='profiles' and path[2]=='leader_models'
                     and path[4] in {'test_pf','train_pf','profit_factor'} and value==math.inf)
            if not allowed:raise ValueError('Non-finite non-analytical state requires explicit investigation')
            changed.append(path)
            return 'Infinity'  # Preserve the ratio semantics in valid JSON.
        return value
    return visit(result,[]),changed

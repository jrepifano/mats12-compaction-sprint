"""Read a torch .pt zip's data.pkl WITHOUT executing it: a restricted Unpickler that never imports
anything (find_class returns a placeholder) and replaces tensor storages with placeholders.
Usage: python -I safe_unpickle.py <file.pt> <out.json>"""
import json
import pickle
import sys
import zipfile


class Placeholder:
    def __init__(self, name, args=()):
        self.name, self.args = name, args

    def __call__(self, *a, **k):
        return Placeholder(self.name, a)

    def __setstate__(self, s):
        self.state = s

    def __repr__(self):
        return f"<{self.name}{self.args!r}>"


class Safe(pickle.Unpickler):
    def find_class(self, module, name):
        return Placeholder(f"{module}.{name}")

    def persistent_load(self, pid):
        return Placeholder("storage", tuple(str(x) for x in pid))


def to_jsonable(o):
    if isinstance(o, dict):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(v) for v in o]
    if isinstance(o, (int, float, str, bool)) or o is None:
        return o
    if isinstance(o, bytes):
        return o.hex()
    return repr(o)


z = zipfile.ZipFile(sys.argv[1])
name = [n for n in z.namelist() if n.endswith("data.pkl")][0]
obj = Safe(__import__("io").BytesIO(z.read(name))).load()
json.dump(to_jsonable(obj), open(sys.argv[2], "w"), indent=1)
print(json.dumps(to_jsonable(obj))[:4000])

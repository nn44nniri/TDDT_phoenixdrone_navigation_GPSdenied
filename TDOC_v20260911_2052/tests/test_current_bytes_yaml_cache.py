import os
from tddt_tdoc.model_compatibility import read_safe_yaml

def test_semantic_reuse_never_memoizes_file_bytes_or_shares_mutable_state(tmp_path):
    p=tmp_path/'profile.yaml';p.write_text('value: 1\nitems: [1, 2]\n');stat=p.stat()
    first=read_safe_yaml(p);first['items'].append(3)
    assert read_safe_yaml(p)['items']==[1,2]
    p.write_text('value: 2\nitems: [1, 2]\n');os.utime(p,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    assert p.stat().st_size==stat.st_size
    assert read_safe_yaml(p)['value']==2

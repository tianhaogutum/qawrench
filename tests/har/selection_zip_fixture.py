"""Synthetic selection archive, independent of the package under test."""
import copy
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def fixture():
    data = json.loads((ROOT/'examples/har/selection-source.har').read_bytes())
    wanted = json.loads((ROOT/'examples/har/selection-requests.json').read_bytes())
    shared = 'Payload/é.bin'
    post = data['log']['entries'][1]['request']['postData']
    post.update(text='', _file=shared)  # official empty placeholder retained
    content = data['log']['entries'][3]['response']['content']
    content.pop('encoding')
    content.pop('text')
    content.update(_file=shared, size=3)
    late = data['log']['entries'][5]['response']['content']
    late.pop('text')
    late.update(_file='late.bin')
    # Manifest deliberately between payloads, with unreferenced members first/last.
    members = {'unused.bin': b'unused\x00\xff', shared: 'é\x00'.encode(),
               'capture.har': None, 'late.bin': b'other', 'trailing.bin': b'never retained'}
    expected = copy.deepcopy(data)
    expected['log']['entries'] = [expected['log']['entries'][i] for i in (1, 3)]
    return data, wanted, members, expected


def write_zip(path, data, members):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name, body in members.items():
            archive.writestr(name, json.dumps(data, ensure_ascii=False) if name == 'capture.har' else body)

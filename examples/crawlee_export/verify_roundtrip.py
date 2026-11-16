"""Independent fixture assertion using only PyArrow + stdlib (no exporter imports).

This script checks the bundled SYNTHETIC dataset's exact expected values.
It is not a general verifier for arbitrary user datasets; use the CLI --verify.
"""
import hashlib
import json
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.parquet as pq


def main():
    path = Path(sys.argv[1])
    manifest = json.loads(Path(str(path) + '.manifest.json').read_text())
    with pq.ParquetFile(path) as parquet:
        rows = parquet.read(use_threads=False).to_pylist()
        assert parquet.schema_arrow.field('count').type == pa.int64()
        assert parquet.schema_arrow.field('price').type == pa.float64()
        assert parquet.schema_arrow.field('active').type == pa.bool_()
    expected = [
        {'title': 'A, B\nC "quoted" — café 東京', 'price': 12.5,
         'count': 9223372036854775807, 'active': True, 'note': None,
         'tags': '["x",{"a":"é\\n","z":2}]'},
        {'title': '', 'price': 0.0, 'count': -9223372036854775808,
         'active': False, 'note': '', 'tags': '[]'},
        {'title': 'last', 'price': -0.0, 'count': 9007199254740993,
         'active': True, 'note': 'line\nnext', 'tags': '{"nested":{"a":null,"b":false}}'},
    ]
    for row, name in zip(expected, ['000000001.json', '000000002.json', '000000010.json']):
        row.update(_cpe_source=name, _cpe_item_id=str(int(name[:-5])), _cpe_line=None)
    assert rows == expected, (rows, expected)
    assert len(rows) == manifest['row_count'] == 3
    assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest['parquet_sha256']
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',', ':'),
                                 allow_nan=False).encode('utf-8') + b'\n')
    assert digest.hexdigest() == manifest['rows_sha256']
    print('Verified 3 synthetic rows: exact values, provenance, types, row count and digests.')


if __name__ == '__main__':
    main()

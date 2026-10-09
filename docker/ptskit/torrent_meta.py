"""Bounded bencode parser for exact torrent identity and full content size."""
import hashlib

MAX_META_BYTES = 12 * 1024 * 1024


class MetadataError(ValueError):
    pass


def torrent_metadata(data):
    if not isinstance(data, bytes) or not data or len(data) > MAX_META_BYTES:
        raise MetadataError('invalid_metainfo_length')
    position = 0
    nodes = 0
    info_span = None

    def decode(depth=0):
        nonlocal position, nodes, info_span
        nodes += 1
        if depth > 64 or nodes > 300000 or position >= len(data):
            raise MetadataError('invalid_bencode_structure')
        token = data[position:position + 1]
        if token == b'i':
            end = data.find(b'e', position + 1)
            raw = data[position + 1:end]
            if end < 0 or not raw or raw == b'-0' or (len(raw) > 1 and raw[:1] == b'0'):
                raise MetadataError('invalid_integer')
            digits = raw[1:] if raw.startswith(b'-') else raw
            if not digits.isdigit() or (raw.startswith(b'-') and digits[:1] == b'0'):
                raise MetadataError('invalid_integer')
            position = end + 1
            return int(raw)
        if token in (b'l', b'd'):
            position += 1
            value = [] if token == b'l' else {}
            while position < len(data) and data[position:position + 1] != b'e':
                key = decode(depth + 1)
                if token == b'l':
                    value.append(key)
                    continue
                if not isinstance(key, bytes) or key in value:
                    raise MetadataError('invalid_dictionary_key')
                start = position
                value[key] = decode(depth + 1)
                if depth == 0 and key == b'info':
                    info_span = (start, position)
            if position >= len(data):
                raise MetadataError('unterminated_collection')
            position += 1
            return value
        if token.isdigit():
            colon = data.find(b':', position)
            raw_length = data[position:colon]
            if colon < 0 or not raw_length.isdigit() or len(raw_length) > 10:
                raise MetadataError('invalid_string_length')
            position = colon + 1 + int(raw_length)
            if position > len(data):
                raise MetadataError('truncated_string')
            return data[colon + 1:position]
        raise MetadataError('invalid_bencode_token')

    root = decode()
    if position != len(data) or not isinstance(root, dict) or info_span is None:
        raise MetadataError('invalid_metainfo_root')
    info = root[b'info']
    if not isinstance(info, dict):
        raise MetadataError('invalid_info_dictionary')

    def file_length(value):
        if type(value) is not int or value < 0:
            raise MetadataError('invalid_file_length')
        return value

    totals = []
    hashes = []
    raw_info = data[info_span[0]:info_span[1]]
    if b'pieces' in info:
        if not isinstance(info[b'pieces'], bytes) or len(info[b'pieces']) % 20:
            raise MetadataError('invalid_piece_hashes')
        if b'length' in info:
            totals.append(file_length(info[b'length']))
        elif isinstance(info.get(b'files'), list) and info[b'files']:
            if any(not isinstance(f, dict) or b'length' not in f for f in info[b'files']):
                raise MetadataError('invalid_file_list')
            totals.append(sum(file_length(f[b'length']) for f in info[b'files']))
        else:
            raise MetadataError('missing_v1_lengths')
        hashes.append(hashlib.sha1(raw_info).hexdigest())
    if info.get(b'meta version') == 2:
        def tree_size(branch):
            if not isinstance(branch, dict):
                raise MetadataError('invalid_file_tree')
            total = 0
            for name, child in branch.items():
                if name == b'':
                    if not isinstance(child, dict) or b'length' not in child:
                        raise MetadataError('invalid_v2_length')
                    total += file_length(child[b'length'])
                else:
                    total += tree_size(child)
            return total
        tree = info.get(b'file tree')
        if not isinstance(tree, dict) or not tree:
            raise MetadataError('invalid_file_tree')
        totals.append(tree_size(tree))
        digest = hashlib.sha256(raw_info).hexdigest()
        hashes.extend([digest, digest[:40]])
    if not totals:
        raise MetadataError('missing_file_lengths')
    return {'size': max(totals), 'hashes': hashes}

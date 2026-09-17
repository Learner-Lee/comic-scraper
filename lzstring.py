"""LZString 解压（只实现 decompressFromBase64 这一个方向）。

manhuagui 用它压缩 packer 的字典。算法出自 pieroxy/lz-string，是公开且固定的，
所以宁可自带这 50 行，也不为一个函数引入一个 pip 依赖。

正确性有个现成的硬校验：解压结果 split('|') 的项数，必须等于 packer 的参数 c。
对不上就说明解错了，调用方应当据此报错。
"""
from __future__ import annotations

from typing import Callable

_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


class LZStringError(ValueError):
    """解压失败。"""


def decompress_from_base64(text: str) -> str:
    """解开 LZString.compressToBase64 压出来的串。"""
    if not text:
        return ""
    try:
        return _decompress(len(text), 32, lambda i: _B64.index(text[i]))
    except (ValueError, IndexError, TypeError) as exc:
        raise LZStringError(f"LZString 解压失败: {exc}") from exc


def _decompress(length: int, reset_value: int,
                get_next: Callable[[int], int]) -> str:
    dictionary: list = [0, 1, 2]
    enlarge_in, dict_size, num_bits = 4, 4, 3
    result: list[str] = []

    data_val, data_pos, data_idx = get_next(0), reset_value, 1

    def read(nbits: int) -> int:
        """按位读出一个 nbits 宽的整数。"""
        nonlocal data_val, data_pos, data_idx
        bits, power, maxpower = 0, 1, 1 << nbits
        while power != maxpower:
            resb = data_val & data_pos
            data_pos >>= 1
            if data_pos == 0:
                data_pos = reset_value
                data_val = get_next(data_idx)
                data_idx += 1
            bits |= (1 if resb > 0 else 0) * power
            power <<= 1
        return bits

    head = read(2)
    if head == 2:
        return ""
    if head not in (0, 1):
        raise LZStringError(f"开头的标记不认识: {head}")
    c = chr(read(8 if head == 0 else 16))

    dictionary.append(c)
    w = c
    result.append(c)

    while True:
        if data_idx > length:
            return ""
        code = read(num_bits)
        if code in (0, 1):
            dictionary.append(chr(read(8 if code == 0 else 16)))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 2:
            return "".join(result)

        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

        if code < len(dictionary):
            entry = dictionary[code]
        elif code == dict_size:
            entry = w + w[0]
        else:
            raise LZStringError(f"字典里没有编号 {code}")

        result.append(entry)
        dictionary.append(w + entry[0])
        dict_size += 1
        w = entry
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1

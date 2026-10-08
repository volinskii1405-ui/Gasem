#!/usr/bin/env python3
"""Работа с GasemFS в образе диска GasemOS прямо с компьютера.

    python3 os/gasemfs.py gasemos.img ls               список файлов
    python3 os/gasemfs.py gasemos.img cat note.txt     содержимое файла
    python3 os/gasemfs.py gasemos.img put my.txt       положить файл на диск
    python3 os/gasemfs.py gasemos.img put a.bin b.bin  … под другим именем
    python3 os/gasemfs.py gasemos.img check            проверить целостность

Формат описан в os/fs.gsm.
"""

import os
import struct
import sys

SECTOR = 512
FS_START = 64
FAT_SEC, DIR_SEC, DATA_SEC = FS_START + 1, FS_START + 2, FS_START + 6
BLOCKS, ENTRIES = 256, 64
FAT_END = 0xFFFF
MAGIC = b"GASEMFS1"


class GasemFS:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.image = bytearray(f.read())
        if self.sector(FS_START)[:8] != MAGIC:
            raise SystemExit(f"{path}: файловая система GasemFS не найдена")
        self.fat = list(struct.unpack(f"<{BLOCKS}H", self.sector(FAT_SEC)))
        raw = b"".join(self.sector(DIR_SEC + i) for i in range(4))
        self.entries = []
        for i in range(ENTRIES):
            e = raw[i * 32:(i + 1) * 32]
            name = e[:24].split(b"\0", 1)[0].decode("latin-1")
            size, block = struct.unpack("<IH", e[24:30])
            self.entries.append([name, size, block])

    def sector(self, n):
        return bytes(self.image[n * SECTOR:(n + 1) * SECTOR])

    def chain(self, block):
        out, seen = [], set()
        while block not in (0, FAT_END):
            if block >= BLOCKS or block in seen:
                raise ValueError(f"испорченная цепочка блоков на блоке {block}")
            seen.add(block)
            out.append(block)
            block = self.fat[block]
        return out

    def files(self):
        return [e for e in self.entries if e[0]]

    def read(self, name):
        for n, size, block in self.files():
            if n == name:
                data = b"".join(self.sector(DATA_SEC + b) for b in self.chain(block))
                return data[:size]
        raise SystemExit(f"файл не найден: {name}")

    def check(self):
        """Цепочки не пересекаются, размеры совпадают с числом блоков."""
        used = {}
        for name, size, block in self.files():
            blocks = self.chain(block)
            if len(blocks) != (size + SECTOR - 1) // SECTOR:
                raise ValueError(f"{name}: размер {size} не совпадает с цепочкой из {len(blocks)} блоков")
            for b in blocks:
                if b in used:
                    raise ValueError(f"блок {b} занят и {used[b]}, и {name}")
                used[b] = name
        for b in range(1, BLOCKS):
            if self.fat[b] != 0 and b not in used:
                raise ValueError(f"блок {b} помечен занятым, но не принадлежит ни одному файлу")
        return True

    def put(self, name, data):
        if len(name.encode()) > 23:
            raise SystemExit("имя длиннее 23 символов")
        for e in self.entries:          # перезапись: освобождаем старые блоки
            if e[0] == name:
                for b in self.chain(e[2]):
                    self.fat[b] = 0
                e[0] = ""
        slot = next((e for e in self.entries if not e[0]), None)
        if slot is None:
            raise SystemExit("каталог полон")
        need = (len(data) + SECTOR - 1) // SECTOR
        free = [b for b in range(1, BLOCKS) if self.fat[b] == 0][:need]
        if len(free) < need:
            raise SystemExit("диск полон")
        for i, b in enumerate(free):
            self.fat[b] = free[i + 1] if i + 1 < len(free) else FAT_END
            chunk = data[i * SECTOR:(i + 1) * SECTOR].ljust(SECTOR, b"\0")
            self.image[(DATA_SEC + b) * SECTOR:(DATA_SEC + b + 1) * SECTOR] = chunk
        slot[:] = [name, len(data), free[0] if free else 0]
        self.save()

    def save(self):
        self.image[FAT_SEC * SECTOR:(FAT_SEC + 1) * SECTOR] = struct.pack(f"<{BLOCKS}H", *self.fat)
        raw = b"".join(n.encode().ljust(24, b"\0") + struct.pack("<IH", s, b) + b"\0\0"
                       for n, s, b in self.entries)
        self.image[DIR_SEC * SECTOR:DATA_SEC * SECTOR] = raw
        with open(self.path, "wb") as f:
            f.write(self.image)


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 1
    fs = GasemFS(argv[1])
    cmd = argv[2]
    if cmd == "ls":
        for name, size, _ in fs.files():
            print(f"{name:<24}{size:>7} байт")
    elif cmd == "cat":
        sys.stdout.write(fs.read(argv[3]).decode("utf-8", errors="replace"))
    elif cmd == "put":
        with open(argv[3], "rb") as f:
            fs.put(os.path.basename(argv[3]) if len(argv) < 5 else argv[4], f.read())
    elif cmd == "check":
        fs.check()
        print("GasemFS в порядке")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

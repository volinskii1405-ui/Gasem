package main

// Тексты справки — те же, что выводит Python-версия gasem (argparse, 80 столбцов).

const helpBuild = `usage: gasem build [-h] [-o OUTPUT] [-w] [-q] [-l LISTING] [-m MAP] [--run]
                   input

Собрать программу Gasem.

positional arguments:
  input                 исходный файл (.gsm)

options:
  -h, --help            show this help message and exit
  -o, --output OUTPUT   выходной файл (по умолчанию — имя исходника с .bin)
  -w, --no-warnings     не показывать предупреждения
  -q, --quiet           не печатать сообщение об успехе
  -l, --listing LISTING
                        записать листинг (адреса, байты, исходные строки)
  -m, --map MAP         записать карту символов (адрес и имя каждой метки)
  --run                 после сборки запустить в QEMU
`

const helpRun = `usage: gasem run [-h] [-o OUTPUT] [-w] [-q] input [qemu_args ...]

Собрать и запустить в QEMU.

positional arguments:
  input                исходный файл (.gsm)
  qemu_args            дополнительные параметры QEMU (после --)

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  выходной файл (по умолчанию — имя исходника с .bin)
  -w, --no-warnings    не показывать предупреждения
  -q, --quiet          не печатать сообщение об успехе
`

const helpDebug = `usage: gasem debug [-h] [-o OUTPUT] [-w] [-q] [-b МЕТКА] [--batch] input

Запустить в QEMU под отладчиком GDB. В GDB доступны команды gbreak <метка>,
gstep (одна строка исходника) и gwhere.

positional arguments:
  input                исходный файл (.gsm)

options:
  -h, --help           show this help message and exit
  -o, --output OUTPUT  выходной файл (по умолчанию — имя исходника с .bin)
  -w, --no-warnings    не показывать предупреждения
  -q, --quiet          не печатать сообщение об успехе
  -b, --break МЕТКА    точка останова на метке (можно несколько раз)
  --batch              без окон: дойти до первой точки останова, показать
                       состояние и выйти
`

const helpFmt = `usage: gasem fmt [-h] [--check] [--diff] files [files ...]

Выровнять оформление: отступы в блоках if/while/for и столбец комментариев.
Меняются только пробелы.

positional arguments:
  files       файлы .gsm

options:
  -h, --help  show this help message and exit
  --check     только проверить, ничего не менять
  --diff      показать изменения, ничего не менять
`

# -*- coding: utf-8 -*-
#
# This file is part of the python-shogi library.
# Copyright (C) 2015- Tasuku SUENAGA <tasuku-s-github@titech.ac>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals

import shogi

import os
import codecs
import shutil
import unittest
import tempfile
from mock import patch
from shogi import PSN

TEST_PSN_STR = """[Name "Thomas Majewski"]
[Email ""]
[Country "Japan"]
[Sente "Oyama Yasuharu"]
[Gote "Nakahara Makoto"]
[Black_grade "Kisei"]
[White_grade "Grade"]
[Result "1-0"]
[Comment "Sangenbisha"]
[Source ""]
[Event "26. Osho Sen"]
[Date "19760204"]
[Round "3"]
[Venue ""]
[Proam "Professional"]
P7g-7f P8c-8d R2h-7h P3c-3d P6g-6f P8d-8e B8h-7g S7a-6b S7i-6h K5a-4b K5i-4h K4b-3b K4h-3h G6a-5b K3h-2h P1c-1d P1g-1f P5c-5d S3i-3h S3a-4b G6i-5h P7c-7d P5g-5f S4b-3c S6h-5g B2b-3a R7h-8h S6b-7c B7g-5i P4c-4d P3g-3f K3b-2b P2g-2f G4a-3b S3h-2g P2c-2d G4i-3h G5b-4c B5i-3g B3a-6d P4g-4f B6d-4b G5h-4g P9c-9d P9g-9f L9a-9c S5g-4h R8b-8d P6f-6e P7d-7e P7fx7e B4bx7e S4h-5g R8d-7d P'7f B7e-4b R8h-7h R7d-8d R7h-8h P'7h S5g-6h P6c-6d B3g-4h R8d-8b P6ex6d S7cx6d N8i-7g S6d-7c R8h-8i S7c-7d N2i-3g P7h-7i+ S6hx7i R8b-6b P'6e P'7e P7fx7e S7dx7e S7i-7h P'7f N7gx8e R6bx6e R8i-6i P'6h R6i-7i P7f-7g+ S7hx7g R6e-6g+ G4g-5g +R6g-6e P'6g P6h-6i+ R7ix6i +R6e-7d N8ex9c N8ax9c R6i-7i N'6e S7g-8f N6ex5g+ B4hx5g +R7d-6e S8fx7e +R6ex6g S7e-6f P'7h R7i-2i G'5h P'6h +R6gx8g B5g-3i P7h-7i+ S6f-6e P'7e P5f-5e +R8g-7g P5ex5d +R7gx6h P'6g P4d-4e P5d-5c+ B4bx5c P'5i G5hx5i N'5e G4c-4b P'5d B5c-4d S6e-5f P'5c N3gx4e G5i-4i P5dx5c+ G4ix3i R2ix3i B'4h +P5cx4b S3cx4b G3hx4h +R6hx4h G'3h G'3g K2h-1h +R4hx3i G3hx3i P1d-1e S'4a G3b-3a R'6c S4b-5c R6cx5c+ B4dx5c B'3c N2ax3c N4ex3c+ K2bx3c N'4e K3c-2b G'3c K2b-1c S'1d
"""

TEST_PSN_RESULT = {
    'moves': [
        '7g7f', '8c8d', '2h7h', '3c3d', '6g6f', '8d8e', '8h7g', '7a6b',
        '7i6h', '5a4b', '5i4h', '4b3b', '4h3h', '6a5b', '3h2h', '1c1d',
        '1g1f', '5c5d', '3i3h', '3a4b', '6i5h', '7c7d', '5g5f', '4b3c',
        '6h5g', '2b3a', '7h8h', '6b7c', '7g5i', '4c4d', '3g3f', '3b2b',
        '2g2f', '4a3b', '3h2g', '2c2d', '4i3h', '5b4c', '5i3g', '3a6d',
        '4g4f', '6d4b', '5h4g', '9c9d', '9g9f', '9a9c', '5g4h', '8b8d',
        '6f6e', '7d7e', '7f7e', '4b7e', '4h5g', '8d7d', 'P*7f', '7e4b',
        '8h7h', '7d8d', '7h8h', 'P*7h', '5g6h', '6c6d', '3g4h', '8d8b',
        '6e6d', '7c6d', '8i7g', '6d7c', '8h8i', '7c7d', '2i3g', '7h7i+',
        '6h7i', '8b6b', 'P*6e', 'P*7e', '7f7e', '7d7e', '7i7h', 'P*7f',
        '7g8e', '6b6e', '8i6i', 'P*6h', '6i7i', '7f7g+', '7h7g', '6e6g+',
        '4g5g', '6g6e', 'P*6g', '6h6i+', '7i6i', '6e7d', '8e9c', '8a9c',
        '6i7i', 'N*6e', '7g8f', '6e5g+', '4h5g', '7d6e', '8f7e', '6e6g',
        '7e6f', 'P*7h', '7i2i', 'G*5h', 'P*6h', '6g8g', '5g3i', '7h7i+',
        '6f6e', 'P*7e', '5f5e', '8g7g', '5e5d', '7g6h', 'P*6g', '4d4e',
        '5d5c+', '4b5c', 'P*5i', '5h5i', 'N*5e', '4c4b', 'P*5d', '5c4d',
        '6e5f', 'P*5c', '3g4e', '5i4i', '5d5c+', '4i3i', '2i3i', 'B*4h',
        '5c4b', '3c4b', '3h4h', '6h4h', 'G*3h', 'G*3g', '2h1h', '4h3i',
        '3h3i', '1d1e', 'S*4a', '3b3a', 'R*6c', '4b5c', '6c5c+', '4d5c',
        'B*3c', '2a3c', '4e3c+', '2b3c', 'N*4e', '3c2b', 'G*3c', '2b1c',
        'S*1d'
    ],
    'names': ['Oyama Yasuharu', 'Nakahara Makoto'],
    'sfen': 'lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1',
    'win': 'b'
}

class ParserTest(unittest.TestCase):
    def parse_str_test(self):
        self.maxDiff = None
        result = PSN.Parser.parse_str(TEST_PSN_STR)
        self.assertEqual(result[0], TEST_PSN_RESULT)

    def parse_file_test(self):
        try:
            tempdir = tempfile.mkdtemp()

            # .psn
            path = os.path.join(tempdir, 'test1.psn')
            with codecs.open(path, 'w', 'cp932') as f:
                f.write(TEST_PSN_STR)
            result = PSN.Parser.parse_file(path)
            self.assertEqual(result[0], TEST_PSN_RESULT)
        finally:
            shutil.rmtree(tempdir)

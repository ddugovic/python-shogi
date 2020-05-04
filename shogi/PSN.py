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

import os
import re
import shogi
import codecs

class ParserException(Exception):
    pass

class Parser:
    TAG_RE = re.compile(r'\[(\w+) "(.*)"\]')

    MOVE_RE = re.compile(r'\A(\+?\w)(?:([1-9])([a-i])[-x]|(\'))([1-9])([a-i])(\+?)\Z')

    HANDICAP_SFENS = {
        'Even': shogi.STARTING_SFEN,
        'Sente': 'lnsgkgsn1/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Lance': '1nsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Bishop': 'lnsgkgsnl/1r7/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Rook': 'lnsgkgsnl/7b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Rook and Lance': 'lnsgkgsn1/7b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Two Pieces': 'lnsgkgsnl/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Three Pieces': 'lnsgkgsn1/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Four Pieces': '1nsgkgsn1/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Five Pieces': '2sgkgsn1/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Five Pieces Right': '1nsgkgs2/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Six Pieces': '2sgkgs2/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Seven Pieces': '3gkgs2/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Eight Pieces': '3gkg3/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Nine Pieces': '4kg3/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Ten Pieces': '4k4/9/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL w - 1',
        'Joseki': None
    }

    @staticmethod
    def parse_file(path):
        prefix, ext = os.path.splitext(path)
        enc = 'cp932'
        with codecs.open(path, 'r', enc) as f:
            return Parser.parse_str(f.read())

    @staticmethod
    def parse_pieces_in_hand(target):
        if target == 'None':
            return {}

        result = {}
        for item in target.split(' '):
            if len(item) == 1:
                result[shogi.PIECE_SYMBOLS.index(item)] = 1
            elif len(item) == 2 or len(item) == 3:
                result[shogi.PIECE_SYMBOLS.index(item[0])] = \
                    shogi.NUMBER_KANJI_SYMBOLS.index(item[1:])
            elif len(item) == 0:
                pass
            else:
                raise ParserException('Invalid pieces in hand')
        return result

    @staticmethod
    def parse_move_str(line, last_to_square):
        m = Parser.MOVE_RE.match(line)
        if m:
            piece_type = shogi.PIECE_SYMBOLS.index(m.group(1).lower())
            to_field = 9 - int(m.group(5))
            to_rank = shogi.NUMBER_ENGLISH_ALPHA_SYMBOLS.index(m.group(6))
            to_square = to_rank * 9 + to_field
            last_to_square = to_square

            if m.group(4) == '\'':
                # piece drop
                return ('{0}*{1}'.format(shogi.PIECE_SYMBOLS[piece_type].upper(),
                    shogi.SQUARE_NAMES[to_square]), last_to_square)
            else:
                from_field = 9 - int(m.group(2))
                from_rank = shogi.NUMBER_ENGLISH_ALPHA_SYMBOLS.index(m.group(3))
                from_square = from_rank * 9 + from_field

                promotion = (m.group(7) == '+')
                return (shogi.SQUARE_NAMES[from_square] + shogi.SQUARE_NAMES[to_square] + ('+' if promotion else ''), last_to_square)
        return (None, last_to_square)

    @staticmethod
    def parse_str(psn_str):
        line_no = 1

        names = [None, None]
        pieces_in_hand = [{}, {}]
        current_turn = shogi.BLACK
        sfen = shogi.STARTING_SFEN
        moves = []
        last_to_square = None
        win = None
        psn_str = psn_str.replace('\r\n', '\n').replace('\r', '\n')
        for line in psn_str.split('\n'):
            t = Parser.TAG_RE.match(line)
            if len(line) == 0 or line[0] == "*":
                pass
            elif t:
                key = t.group(1)
                value = t.group(2)
                if key == 'Sente': # sente or shitate
                    # Black's name
                    names[shogi.BLACK] = value
                elif key == 'Gote': # gote or uwate
                    # White's name
                    names[shogi.WHITE] = value
                elif key == 'Result':
                    # Game result
                    win = 'b' if value == "1-0" else 'w' if value == "0-1" else '-'
                elif key == 'Sente_hand':
                    # First player's pieces in hand
                    pieces_in_hand[shogi.BLACK] == Parser.parse_pieces_in_hand(value)
                elif key == 'Gote_hand':
                    # Second player's pieces in hand
                    pieces_in_hand[shogi.WHITE] == Parser.parse_pieces_in_hand(value)
                elif key == 'Handicap':
                    sfen = Parser.HANDICAP_SFENS[value]
                    if sfen is None:
                        raise ParserException('Cannot support handicap type "other"')
            elif line == '...':
                # Current turn is white
                current_turn = shogi.WHITE
            else:
                for word in line.split(' '):
                    (move, last_to_square) = Parser.parse_move_str(word, last_to_square)
                    if move is not None:
                        moves.append(move)
            line_no += 1

        summary = {
            'names': names,
            'sfen': sfen,
            'moves': moves,
            'win': win
        }

        # NOTE: for the same interface with CSA parser
        return [summary]

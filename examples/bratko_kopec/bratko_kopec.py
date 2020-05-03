#!/usr/bin/env python

"""Run an EPD test suite with a USI engine."""

import asyncio
import time
import argparse
import itertools
import logging
import sys

import shogi
import shogi.engine


async def test_sfen(engine, sfen, movetime):
    board, sfen_info = shogi.Board(sfen)
    sfen_string = sfen_info.get("id", board.fen())
    if "am" in sfen_info:
        sfen_string = "{} (avoid {})".format(sfen_string, " and ".join(board.san(am) for am in sfen_info["am"]))
    if "bm" in sfen_info:
        sfen_string = "{} (expect {})".format(sfen_string, " or ".join(board.san(bm) for bm in sfen_info["bm"]))

    limit = shogi.engine.Limit(time=movetime)
    result = await engine.play(board, limit, game=object())

    if "am" in sfen_info and result.move in sfen_info["am"]:
        print(f"{sfen_string}: {board.san(result.move)} | +0")
        return 0.0
    elif "bm" in sfen_info and result.move not in sfen_info["bm"]:
        print(f"{sfen_string}: {board.san(result.move)} | +0")
        return 0.0
    else:
        print(f"{sfen_string}: {board.san(result.move)} | +1")
        return 1.0


async def test_sfen_with_fractional_scores(engine, sfen, movetime):
    board, sfen_info = shogi.Board(sfen)
    sfen_string = sfen_info.get("id", board.fen())
    if "am" in sfen_info:
        sfen_string = "{} (avoid {})".format(sfen_string, " and ".join(board.san(am) for am in sfen_info["am"]))
    if "bm" in sfen_info:
        sfen_string = "{} (expect {})".format(sfen_string, " or ".join(board.san(bm) for bm in sfen_info["bm"]))

    # Start analysis.
    score = 0.0
    print(f"{sfen_string}:", end=" ", flush=True)
    analysis = await engine.analysis(board, game=object())

    with analysis:
        for step in range(0, 4):
            await asyncio.sleep(movetime / 4)

            # Assess the current principal variation.
            if "pv" in analysis.info and len(analysis.info["pv"]) >= 1:
                move = analysis.info["pv"][0]
                print(board.san(move), end=" ", flush=True)
                if "am" in sfen_info and move in sfen_info["am"]:
                    continue  # fail
                elif "bm" in sfen_info and move not in sfen_info["bm"]:
                    continue  # fail
                else:
                    score = 1.0 / (4 - step)
            else:
                print("(no pv)", end=" ", flush=True)

    # Done.
    print(f"| +{score}")
    return score


async def main():
    # Parse command line arguments.
    parser = argparse.ArgumentParser(description=__doc__)

    engine_group = parser.add_mutually_exclusive_group(required=True)
    engine_group.add_argument("-u", "--usi",
        help="The USI engine under test.")

    parser.add_argument("sfen", nargs="+", type=argparse.FileType("r"),
        help="EPD test suite(s).")
    parser.add_argument("-t", "--threads", default=1, type=int,
        help="Threads for use by the USI engine.")
    parser.add_argument("-m", "--movetime", default=1.0, type=float,
        help="Time to move in milliseconds.")
    parser.add_argument("-s", "--simple", dest="test_sfen", action="store_const",
        default=test_sfen_with_fractional_scores,
        const=test_sfen,
        help="Run in simple mode without fractional scores.")
    parser.add_argument("-d", "--debug", action="store_true",
        help="Show debug logs.")

    args = parser.parse_args()

    # Configure logger.
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)

    # Open and configure engine.
    _, engine = await shogi.engine.popen_usi(args.usi)
    if args.threads > 1:
        await engine.configure({"Threads": args.threads})

    # Run each test line.
    score = 0.0
    count = 0

    for sfen in itertools.chain(*args.sfen):
        # Skip comments and empty lines.
        sfen = sfen.strip()
        if not sfen or sfen.startswith("#") or sfen.startswith("%"):
            print(sfen.rstrip())
            continue

        # Run the actual test.
        score += await args.test_sfen(engine, sfen, args.movetime)
        count += 1

    await engine.quit()

    print("-------------------------------")
    print(f"{score} / {count}")


if __name__ == "__main__":
    asyncio.set_event_loop_policy(shogi.engine.EventLoopPolicy())
    asyncio.run(main())

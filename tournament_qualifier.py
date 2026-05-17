#!/usr/bin/env python3
"""
Tournament Qualification Calculator
====================================
Calculates qualification scenarios for any round-robin tournament.

Works by:
  - Full enumeration when remaining matches are few (≤ ENUM_LIMIT)
  - Monte Carlo simulation for larger match counts

Supports any tournament format — IPL, Premier League, World Cup groups, etc.

Usage:
    python tournament_qualifier.py                  # Full report
    python tournament_qualifier.py --team MI        # Focus on one team
    python tournament_qualifier.py --list-teams     # Show team codes

Update the TOURNAMENT DATA section at the bottom for your tournament.
"""

from __future__ import annotations

import argparse
import copy
import random
from dataclasses import dataclass, field
from itertools import product
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Team:
    """Represents a team and their current league-stage record."""
    name: str
    short_name: str   # 2–4 char code used in match fixtures
    played: int
    won: int
    lost: int
    tied: int = 0
    no_result: int = 0
    nrr: float = 0.0  # Net Run Rate (or goal difference, point differential, etc.)

    @property
    def points(self) -> int:
        return self.won * 2 + self.tied + self.no_result

    def remaining_matches(self, all_remaining: list[Match]) -> list[Match]:
        return [m for m in all_remaining
                if m.team1 == self.short_name or m.team2 == self.short_name]

    def max_points(self, all_remaining: list[Match]) -> int:
        return self.points + len(self.remaining_matches(all_remaining)) * 2


@dataclass
class Match:
    """An unplayed fixture between two teams."""
    team1: str   # short_name
    team2: str   # short_name
    match_num: int = 0
    date: str = ""
    venue: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Core Engine
# ─────────────────────────────────────────────────────────────────────────────

class TournamentQualifier:
    """
    Generic qualification analyzer for round-robin tournaments.

    Parameters
    ----------
    teams            : list of Team objects (current standings)
    remaining_matches: list of Match objects (unplayed fixtures)
    qualify_spots    : how many teams advance (e.g. top 4 in IPL)
    allow_ties       : whether matches can end in a tie/draw
    tournament_name  : display name
    """

    ENUM_LIMIT = 18          # exact enumeration for ≤ this many remaining matches (~4s)
    MC_SAMPLES = 500_000     # Monte Carlo samples when enumeration is infeasible

    def __init__(
        self,
        teams: list[Team],
        remaining_matches: list[Match],
        qualify_spots: int,
        allow_ties: bool = False,
        tournament_name: str = "Tournament",
    ):
        self.all_teams: dict[str, Team] = {t.short_name: t for t in teams}
        self.remaining = remaining_matches
        self.qualify_spots = qualify_spots
        self.allow_ties = allow_ties
        self.name = tournament_name
        self._outcomes = [0, 1, 2] if allow_ties else [0, 1]
        # 0 = team1 wins, 1 = team2 wins, 2 = tie/draw

    # ── Fast compact state (avoids deepcopy in hot loops) ────────────────────

    def _build_fast_state(self):
        """
        Returns compact arrays for fast enumeration / simulation.
        All arithmetic is done on plain lists of ints — no dict copies.
        """
        order = list(self.all_teams.keys())
        idx = {sn: i for i, sn in enumerate(order)}
        base_pts = [self.all_teams[sn].points for sn in order]
        base_nrr = [self.all_teams[sn].nrr   for sn in order]
        # Precompute (team1_idx, team2_idx) per match
        deltas = [(idx[m.team1], idx[m.team2]) for m in self.remaining]
        return order, idx, base_pts, base_nrr, deltas

    def _fast_qualifies(self, pts: list, nrr: list, target_i: int) -> bool:
        tp, tn = pts[target_i], nrr[target_i]
        above = sum(
            1 for i, (p, n) in enumerate(zip(pts, nrr))
            if i != target_i and (p > tp or (p == tp and n > tn))
        )
        return above < self.qualify_spots

    # ── Enumeration & Monte Carlo ─────────────────────────────────────────────

    def _run_enumeration(self) -> None:
        """
        Single-pass exact enumeration across ALL teams simultaneously.
        Results are cached in self._enum_cache so each team's analyze_team()
        call is O(1) after the first.
        """
        order, idx, base_pts, base_nrr, deltas = self._build_fast_state()
        n_teams = len(order)
        n_matches = len(self.remaining)

        # Per-team accumulators
        qualify_count  = [0] * n_teams
        min_wins_seen: list[Optional[int]] = [None] * n_teams
        seen_own:  list[set]  = [set()  for _ in order]
        samples:   list[list] = [[]     for _ in order]

        own_indices = [
            [i for i, m in enumerate(self.remaining)
             if m.team1 == sn or m.team2 == sn]
            for sn in order
        ]

        total = 0
        for combo in product(self._outcomes, repeat=n_matches):
            pts = list(base_pts)
            for (t1, t2), oc in zip(deltas, combo):
                if oc == 0:   pts[t1] += 2
                elif oc == 1: pts[t2] += 2
                else:         pts[t1] += 1; pts[t2] += 1
            total += 1

            for ti, sn in enumerate(order):
                if not self._fast_qualifies(pts, base_nrr, ti):
                    continue
                qualify_count[ti] += 1
                ow = sum(
                    1 for i in own_indices[ti]
                    if (self.remaining[i].team1 == sn and combo[i] == 0)
                    or (self.remaining[i].team2 == sn and combo[i] == 1)
                )
                if min_wins_seen[ti] is None or ow < min_wins_seen[ti]:
                    min_wins_seen[ti] = ow
                if len(samples[ti]) < 3:
                    desc = self._describe_own(combo, sn)
                    if desc not in seen_own[ti]:
                        seen_own[ti].add(desc)
                        samples[ti].append(desc)

        self._enum_cache = {
            sn: dict(
                method="exact",
                qualify=qualify_count[ti],
                total=total,
                pct=qualify_count[ti] / total * 100,
                samples=samples[ti],
                exact_min_wins=min_wins_seen[ti],
            )
            for ti, sn in enumerate(order)
        }

    def _enumerate(self, short_name: str) -> dict:
        if not hasattr(self, '_enum_cache'):
            self._run_enumeration()
        return self._enum_cache[short_name]

    def _run_monte_carlo_all(self) -> None:
        """
        Shared Monte Carlo pass — one random simulation loop for all teams.
        Uses a single sort per iteration instead of per-team comparison loops.
        """
        order, idx, base_pts, base_nrr, deltas = self._build_fast_state()
        n_teams = len(order)
        n_matches = len(deltas)
        n = self.MC_SAMPLES
        choices = self._outcomes
        qs = self.qualify_spots
        qualify_count = [0] * n_teams

        for _ in range(n):
            pts = list(base_pts)
            for (t1, t2), oc in zip(deltas, random.choices(choices, k=n_matches)):
                if oc == 0:   pts[t1] += 2
                elif oc == 1: pts[t2] += 2
                else:         pts[t1] += 1; pts[t2] += 1
            # Single sort → read off top-qs positions (much cheaper than per-team loops)
            ranked = sorted(range(n_teams),
                            key=lambda i: (pts[i], base_nrr[i]), reverse=True)
            for pos in range(qs):
                qualify_count[ranked[pos]] += 1

        self._mc_cache = {
            sn: dict(
                method="monte_carlo",
                qualify=qualify_count[ti],
                total=n,
                pct=qualify_count[ti] / n * 100,
                samples=[],
                exact_min_wins=None,
            )
            for ti, sn in enumerate(order)
        }

    def _monte_carlo(self, short_name: str) -> dict:
        if not hasattr(self, '_mc_cache'):
            self._run_monte_carlo_all()
        return self._mc_cache[short_name]

    def _describe_own(self, combo: tuple, short_name: str) -> str:
        """Own-match summary for an outcome combination."""
        parts = []
        for match, oc in zip(self.remaining, combo):
            if short_name not in (match.team1, match.team2):
                continue
            opp = match.team2 if match.team1 == short_name else match.team1
            if oc == 0:
                r = "WIN" if match.team1 == short_name else "LOSS"
            elif oc == 1:
                r = "LOSS" if match.team1 == short_name else "WIN"
            else:
                r = "TIE"
            parts.append(f"{r} vs {opp}")
        return " | ".join(parts) if parts else "(no own matches)"

    # ── Greedy checks (fast, works for any match count) ───────────────────────

    def can_still_qualify(self, short_name: str) -> bool:
        """
        Best-case check: can this team possibly finish in the top N?
        Strategy: team wins all remaining; for other matches, make the
        team with higher current points lose (reduce competition).
        """
        order, idx, base_pts, base_nrr, deltas = self._build_fast_state()
        target_i = idx[short_name]
        pts = list(base_pts)

        for i, m in enumerate(self.remaining):
            t1, t2 = deltas[i]
            if m.team1 == short_name:
                pts[t1] += 2                      # target wins
            elif m.team2 == short_name:
                pts[t2] += 2                      # target wins
            elif pts[t1] >= pts[t2]:
                pts[t2] += 2                      # weaker team wins
            else:
                pts[t1] += 2

        return self._fast_qualifies(pts, base_nrr, target_i)

    def already_guaranteed(self, short_name: str) -> bool:
        """
        Worst-case check: is this team guaranteed to qualify no matter what?
        Strategy: team loses all remaining; for other matches, boost the
        team with the lower current points (maximise competition for target).
        """
        order, idx, base_pts, base_nrr, deltas = self._build_fast_state()
        target_i = idx[short_name]
        pts = list(base_pts)

        for i, m in enumerate(self.remaining):
            t1, t2 = deltas[i]
            if m.team1 == short_name:
                pts[t2] += 2                      # target loses
            elif m.team2 == short_name:
                pts[t1] += 2                      # target loses
            elif pts[t1] <= pts[t2]:
                pts[t1] += 2                      # boost lower-ranked team
            else:
                pts[t2] += 2

        return self._fast_qualifies(pts, base_nrr, target_i)

    def min_wins_to_possibly_qualify(self, short_name: str) -> Optional[int]:
        """
        Minimum wins the team needs in their own remaining matches to keep
        qualification mathematically possible.
        Returns None if the team is already eliminated.
        """
        team = self.all_teams[short_name]
        own_left = team.remaining_matches(self.remaining)
        other = [m for m in self.remaining
                 if m.team1 != short_name and m.team2 != short_name]

        for wins in range(len(own_left) + 1):
            pts_with_wins = team.points + wins * 2

            # Best case: all other teams lose as many as possible
            # Count teams that can still mathematically exceed pts_with_wins
            threatening = 0
            for sn, t in self.all_teams.items():
                if sn == short_name:
                    continue
                t_other_left = [m for m in other
                                if m.team1 == sn or m.team2 == sn]
                t_max = t.points + len(t_other_left) * 2
                if t_max > pts_with_wins:
                    threatening += 1

            if threatening < self.qualify_spots:
                return wins

        return None

    def magic_number(self, short_name: str) -> Optional[int]:
        """
        Points needed to guarantee qualification regardless of other results.
        Returns None if already guaranteed, or if it's impossible.
        """
        if self.already_guaranteed(short_name):
            return 0

        team = self.all_teams[short_name]
        own_left = team.remaining_matches(self.remaining)

        # Binary-search for the guaranteed threshold
        for extra_pts in range(0, len(own_left) * 2 + 1):
            hypothetical_pts = team.points + extra_pts

            # Can more than (qualify_spots-1) other teams exceed this?
            # Give every other team their max possible points
            max_others_above = 0
            for sn, t in self.all_teams.items():
                if sn == short_name:
                    continue
                t_max = t.max_points(self.remaining)
                if t_max > hypothetical_pts:
                    max_others_above += 1

            if max_others_above < self.qualify_spots:
                # extra_pts guarantees qualification
                points_still_needed = max(0, hypothetical_pts - team.points)
                return points_still_needed

        return None  # Cannot guarantee qualification

    # ── Full Report ───────────────────────────────────────────────────────────

    def standings(self) -> list[Team]:
        return sorted(self.all_teams.values(),
                      key=lambda t: (t.points, t.nrr), reverse=True)

    def print_standings(self) -> None:
        W = 72
        SEP = "─" * W
        print(f"\n{SEP}")
        print(f"  {'CURRENT STANDINGS':^{W-4}}")
        print(SEP)
        header = (f"{'#':<3} {'Team':<26} {'P':>3} {'W':>3} {'L':>3} "
                  f"{'T':>3} {'NR':>3} {'Pts':>4} {'NRR':>7} {'MaxPts':>7}")
        print(header)
        print(SEP)

        q_line_drawn = False
        for rank, team in enumerate(self.standings(), 1):
            if rank == self.qualify_spots + 1 and not q_line_drawn:
                print(f"  {'· · · QUALIFICATION LINE · · ·':^{W-4}}")
                q_line_drawn = True
            in_zone = rank <= self.qualify_spots
            marker = "►" if in_zone else " "
            max_p = team.max_points(self.remaining)
            print(
                f"{rank:<3} {marker}{team.name:<25} {team.played:>3} "
                f"{team.won:>3} {team.lost:>3} {team.tied:>3} "
                f"{team.no_result:>3} {team.points:>4} "
                f"{team.nrr:>+7.3f} {max_p:>7}"
            )
        print(SEP)
        print(f"  ► = currently in top {self.qualify_spots}  |  "
              f"MaxPts = points if team wins all remaining\n")

    def analyze_team(self, short_name: str) -> dict:
        team = self.all_teams[short_name]
        own_left = team.remaining_matches(self.remaining)

        eliminated = not self.can_still_qualify(short_name)
        guaranteed = self.already_guaranteed(short_name)

        if eliminated or guaranteed:
            stats = dict(method="greedy", qualify=None, total=None,
                         pct=0.0 if eliminated else 100.0, samples=[],
                         exact_min_wins=None)
        elif len(self.remaining) <= self.ENUM_LIMIT:
            stats = self._enumerate(short_name)
        else:
            stats = self._monte_carlo(short_name)
            stats['exact_min_wins'] = None

        # Prefer exact min_wins from enumeration; fall back to greedy estimate
        exact = stats.pop('exact_min_wins', None)
        min_wins = exact if exact is not None else self.min_wins_to_possibly_qualify(short_name)
        min_wins_exact = exact is not None

        return dict(
            team=team,
            own_left=len(own_left),
            max_pts=team.max_points(self.remaining),
            eliminated=eliminated,
            guaranteed=guaranteed,
            min_wins=min_wins,
            min_wins_exact=min_wins_exact,
            magic_number=self.magic_number(short_name),
            **stats,
        )

    def print_team_analysis(self, short_name: str) -> None:
        W = 72
        SEP = "─" * W
        a = self.analyze_team(short_name)
        team = a['team']

        print(f"\n{SEP}")
        print(f"  {team.name}  ({team.short_name})")
        print(SEP)

        if a['eliminated']:
            print(f"  STATUS : ✗ ELIMINATED")
            print(f"  Maximum possible points ({a['max_pts']}) cannot reach "
                  f"qualification zone.\n")
            return

        if a['guaranteed']:
            print(f"  STATUS : ✓ QUALIFIED  (mathematically guaranteed)\n")
            return

        # Determine outlook
        pct = a['pct']
        if pct >= 75:
            outlook = "STRONG"
        elif pct >= 50:
            outlook = "LIKELY"
        elif pct >= 25:
            outlook = "POSSIBLE"
        else:
            outlook = "SLIM"

        method_tag = {
            "exact": "exact enumeration",
            "monte_carlo": f"est. from {self.MC_SAMPLES:,} simulations",
            "greedy": "greedy analysis",
        }[a['method']]

        print(f"  Current points : {team.points}")
        print(f"  Maximum points : {a['max_pts']}")
        print(f"  Own matches left: {a['own_left']}")

        mn = a['magic_number']
        if mn is not None and mn > 0:
            print(f"  Magic number   : {mn} pts to guarantee qualification")
        elif mn == 0:
            print(f"  Magic number   : Already guaranteed!")

        mw = a['min_wins']
        if mw is not None:
            label = "exact" if a.get('min_wins_exact') else "est."
            print(f"  Minimum wins   : {mw} of {a['own_left']} remaining  [{label}]")

        print(f"  Qualify in     : {pct:.1f}% of scenarios  [{method_tag}]")
        print(f"  Outlook        : {outlook}")

        if a['samples']:
            print(f"\n  Sample qualifying scenarios (own-match results):")
            for i, s in enumerate(a['samples'], 1):
                print(f"    {i}. {s}")

        remaining_own = team.remaining_matches(self.remaining)
        if remaining_own:
            print(f"\n  Upcoming fixtures:")
            for m in remaining_own:
                opp = m.team2 if m.team1 == short_name else m.team1
                opp_team = self.all_teams[opp]
                opp_pts = opp_team.points
                label = f"  Match #{m.match_num}" if m.match_num else ""
                date_str = f"  {m.date}" if m.date else ""
                print(f"    vs {opp_team.name:<26} (currently {opp_pts} pts){label}{date_str}")

        print()

    def print_report(self) -> None:
        W = 72
        print(f"\n{'═' * W}")
        print(f"  {self.name.upper()}")
        print(f"  Qualification Calculator  —  Top {self.qualify_spots} advance")
        print(f"{'═' * W}")

        self.print_standings()

        print(f"\n{'═' * W}")
        print(f"  TEAM-BY-TEAM QUALIFICATION ANALYSIS")
        print(f"{'═' * W}")

        for team in self.standings():
            self.print_team_analysis(team.short_name)

        print(f"{'═' * W}\n")

    def print_team_focused(self, short_name: str) -> None:
        """Print standings + detailed analysis for one team."""
        W = 72
        print(f"\n{'═' * W}")
        print(f"  {self.name.upper()}")
        print(f"  Focused Analysis: {self.all_teams[short_name].name}")
        print(f"{'═' * W}")
        self.print_standings()
        self.print_team_analysis(short_name)


# ─────────────────────────────────────────────────────────────────────────────
# ██████████████████████  TOURNAMENT DATA  ████████████████████████████████████
# ─────────────────────────────────────────────────────────────────────────────
#
#  Update this section with your current standings and remaining fixtures.
#  The data below is a sample mid-season snapshot for IPL 2026.
#
#  Team(name, short_name, played, won, lost, tied, no_result, nrr)
#  Match(team1_short, team2_short, match_num, date, venue)   ← optional fields
#
# ─────────────────────────────────────────────────────────────────────────────

IPL_2026_TEAMS = [
    # name                          code   P   W   L  Tie  NR   NRR
    Team("Mumbai Indians",          "MI",  10,  7,  3,  0,  0, +0.652),
    Team("Royal Challengers Bengaluru", "RCB", 10, 6,  4,  0,  0, +0.423),
    Team("Kolkata Knight Riders",   "KKR", 10,  6,  4,  0,  0, +0.318),
    Team("Rajasthan Royals",        "RR",  10,  6,  4,  0,  0, +0.211),
    Team("Delhi Capitals",          "DC",  10,  5,  5,  0,  0, +0.087),
    Team("Chennai Super Kings",     "CSK", 10,  5,  5,  0,  0, -0.105),
    Team("Sunrisers Hyderabad",     "SRH", 10,  4,  6,  0,  0, -0.234),
    Team("Gujarat Titans",          "GT",  10,  4,  6,  0,  0, -0.312),
    Team("Lucknow Super Giants",    "LSG", 10,  3,  7,  0,  0, -0.488),
    Team("Punjab Kings",            "PBKS",10,  2,  8,  0,  0, -0.615),
]

# Each team plays 14 matches — remaining 4 matches each (40 total remaining)
# (Update match numbers and dates to reflect actual fixtures)
IPL_2026_REMAINING = [
    Match("MI",   "CSK",  51, "May 18", "Wankhede"),
    Match("RCB",  "SRH",  52, "May 18", "Chinnaswamy"),
    Match("KKR",  "LSG",  53, "May 19", "Eden Gardens"),
    Match("RR",   "GT",   54, "May 19", "Sawai Mansingh"),
    Match("DC",   "PBKS", 55, "May 20", "Arun Jaitley"),
    Match("MI",   "KKR",  56, "May 21", "Wankhede"),
    Match("RCB",  "RR",   57, "May 21", "Chinnaswamy"),
    Match("CSK",  "GT",   58, "May 22", "Chepauk"),
    Match("SRH",  "DC",   59, "May 22", "Rajiv Gandhi"),
    Match("LSG",  "PBKS", 60, "May 23", "Ekana"),
    Match("MI",   "RR",   61, "May 24", "Wankhede"),
    Match("KKR",  "CSK",  62, "May 24", "Eden Gardens"),
    Match("RCB",  "DC",   63, "May 25", "Chinnaswamy"),
    Match("SRH",  "GT",   64, "May 25", "Rajiv Gandhi"),
    Match("MI",   "SRH",  65, "May 26", "Wankhede"),
    Match("RR",   "LSG",  66, "May 26", "Sawai Mansingh"),
    Match("KKR",  "DC",   67, "May 27", "Eden Gardens"),
    Match("CSK",  "PBKS", 68, "May 27", "Chepauk"),
    Match("RCB",  "GT",   69, "May 28", "Chinnaswamy"),
    Match("MI",   "PBKS", 70, "May 28", "Wankhede"),
]


def build_ipl_2026() -> TournamentQualifier:
    return TournamentQualifier(
        teams=IPL_2026_TEAMS,
        remaining_matches=IPL_2026_REMAINING,
        qualify_spots=4,
        allow_ties=False,   # IPL uses Super Over for ties — no draw points
        tournament_name="IPL 2026 — Indian Premier League",
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tournament Qualification Calculator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tournament_qualifier.py                  Full report (all teams)
  python tournament_qualifier.py --team MI        Focused report for MI
  python tournament_qualifier.py --list-teams     Show available team codes
        """,
    )
    parser.add_argument("--team", metavar="CODE",
                        help="Show focused analysis for one team (use short code)")
    parser.add_argument("--list-teams", action="store_true",
                        help="List all team codes and exit")
    args = parser.parse_args()

    # ── Build your tournament here ────────────────────────────────────────────
    tournament = build_ipl_2026()
    # ── Or call TournamentQualifier() directly with your own data ─────────────

    if args.list_teams:
        print(f"\n{tournament.name}")
        print("Available team codes:")
        for sn, t in tournament.all_teams.items():
            print(f"  {sn:<6}  {t.name}")
        print()
        return

    if args.team:
        code = args.team.upper()
        if code not in tournament.all_teams:
            print(f"Team '{code}' not found. Use --list-teams to see valid codes.")
            return
        tournament.print_team_focused(code)
    else:
        tournament.print_report()


if __name__ == "__main__":
    main()

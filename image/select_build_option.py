#!/usr/bin/env python3
"""Print the ./configure menu number matching a compiler description.

WRF and WPS number their configure options by position in a menu that is
filtered to the detected OS/arch and grows between releases, so the number
for "GNU dm+sm" is not a stable identifier — it moved when we added a
toolchain, and it would move again on a WRF upgrade. Selecting "33" that way
fails silently in the worst possible manner: the build succeeds against a
different compiler or parallelism mode than intended.

This runs configure once with an exit sentinel, parses the menu it prints,
and returns the number whose description matches, so the build asks for what
it means ("GNU, dm+sm") and fails loudly when that is not on offer.

  select_build_option.py wrf 'GNU \\(gfortran/gcc\\)' dm+sm
  select_build_option.py wps 'gfortran' serial

WRF lists every parallelism mode for a compiler on one line:
   32. (serial)  33. (smpar)  34. (dmpar)  35. (dm+sm)   GNU (gfortran/gcc)
WPS gives each mode its own line:
    1.  Linux x86_64, gfortran    (serial)
"""

import re
import subprocess
import sys


def configure_menu():
    """Menu text from ./configure. Feeding -1 makes it print and exit
    without writing a configure.* file (arch/Config.pl treats -1 as quit)."""
    proc = subprocess.run(["./configure"], input="-1\n", capture_output=True,
                          text=True, timeout=300)
    return proc.stdout + proc.stderr


def find_option(menu, description, parallel):
    """Menu number for (description, parallel), or None."""
    desc = re.compile(description)
    mode = re.escape(parallel)
    for line in menu.splitlines():
        if not desc.search(line):
            continue
        # WRF puts every mode for a compiler on the description's line, each
        # directly after its own number: "34. (dmpar)  35. (dm+sm)  GNU ...".
        m = re.search(rf"(\d+)\.\s*\({mode}\)", line)
        if m:
            return int(m.group(1))
        # WPS gives each mode its own line, number first and mode last:
        # "  3.  Linux x86_64, gfortran (dmpar)". Anchoring the mode to the
        # end keeps 'serial' from matching the 'serial_NO_GRIB2' variant.
        if re.search(rf"\({mode}\)\s*$", line):
            m = re.match(r"\s*(\d+)\.", line)
            if m:
                return int(m.group(1))
    return None


def main():
    if len(sys.argv) != 4:
        sys.exit(f"usage: {sys.argv[0]} <wrf|wps> <description-regex> <parallel>")
    _, _kind, description, parallel = sys.argv

    menu = configure_menu()
    opt = find_option(menu, description, parallel)
    if opt is None:
        offered = "\n".join(l for l in menu.splitlines() if re.search(r"^\s*\d+\.", l))
        sys.exit(f"no configure option matches /{description}/ + '{parallel}'.\n"
                 f"Menu offered:\n{offered}")
    print(opt)


if __name__ == "__main__":
    main()

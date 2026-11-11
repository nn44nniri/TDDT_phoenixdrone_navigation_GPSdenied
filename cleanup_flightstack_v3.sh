#!/usr/bin/env bash
# cleanup_flightstack_v3.sh - Conservative post-build source/artifact and dev-directory cleanup
# Debian 13; Bash 5+. Run --dry-run first; --apply to delete identified files.
set -Eeuo pipefail
shopt -s nullglob

MODE=dry-run
BASE="$PWD"
AP=""
JS=""
LOG=""
COUNT=0
BYTES=0
declare -a TARGETS=()
declare -a DIR_TARGETS=()
DIR_COUNT=0
DIR_BYTES=0

usage() {
cat <<'HELP'
Usage:
  ./cleanup_flightstack_v3.sh [--root DIR] [--ardupilot-dir DIR] [--jsbsim-dir DIR] [--dry-run|--apply] [--log FILE]

Examples:
  ./cleanup_flightstack_v3.sh --root /path/to/FlightStack
  ./cleanup_flightstack_v3.sh --root /path/to/FlightStack --apply
  ./cleanup_flightstack_v3.sh --ardupilot-dir /path/to/ardupilot --jsbsim-dir /path/to/jsbsim --apply

By default ONLY prints the cleanup plan. --apply deletes source-code files and
known compilation intermediates AFTER checking JSBSim and ArduPilot SITL binaries.
Removes .git, .vscode, venv, .venv and venv-ardupilot-debian13 directories within both projects.
Does not remove the main project directory structure, model XML, aircraft/engine/systems,
Python/shell launchers, parameter files, logs, shared libraries, or executables.
Keep a full source backup before --apply. Recompilation requires source recovery.
HELP
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
msg() { printf '%s\n' "$*"; }
while (($#)); do
 case "$1" in
  --root|--ardupilot-dir|--jsbsim-dir|--log)
    (($#>=2)) || die "Missing value for $1"
    case "$1" in
      --root) BASE=$2;; --ardupilot-dir) AP=$2;; --jsbsim-dir) JS=$2;; --log) LOG=$2;;
    esac
    shift 2;;
  --apply) MODE=apply; shift;;
  --dry-run) MODE=dry-run; shift;;
  -h|--help) usage; exit 0;;
  *) die "Unknown argument: $1";;
 esac
done
canonical() { realpath -e -- "$1"; }
BASE=$(canonical "$BASE") || die "Invalid root"
if [[ -z $AP ]]; then
 for p in "$BASE/ardupilot" "$BASE/ArduPilot" "$BASE/ardupilot/ardupilot"; do
   if [[ -d $p ]]; then AP=$p; break; fi
 done
fi
if [[ -z $JS ]]; then
 for p in "$BASE/jsbsim" "$BASE/JSBSim" "$BASE/JSBSim-code" "$BASE/ardupilot_JSBsim/jsbsim"; do
   if [[ -d $p ]]; then JS=$p; break; fi
 done
fi
[[ -n $AP && -n $JS ]] || die "Both projects must be located; use --ardupilot-dir and --jsbsim-dir"
AP=$(canonical "$AP"); JS=$(canonical "$JS")
[[ $AP != / && $JS != / && $AP != "$JS" ]] || die "Unsafe paths"
[[ -f $AP/wscript || -d $AP/ArduPlane ]] || die "Directory doesn't look like ArduPilot: $AP"
[[ -f $JS/CMakeLists.txt || -d $JS/aircraft ]] || die "Directory doesn't look like JSBSim: $JS"

find_executable() {
 local root=$1; shift
 local x
 for x in "$@"; do
   [[ -f $root/$x && -x $root/$x && ! -L $root/$x ]] || continue
   if file -b -- "$root/$x" | grep -q 'ELF.*executable\|ELF.*pie executable\|ELF.*shared object'; then
     printf '%s' "$root/$x"; return 0
   fi
 done
 return 1
}
command -v file >/dev/null || die "Install 'file' (apt install file)"
APBIN=$(find_executable "$AP" build/sitl/bin/arduplane build/linux/bin/arduplane) || die "Cannot find compiled ArduPlane executable. Refusing cleanup."
JSBIN=$(find_executable "$JS" build/src/JSBSim src/JSBSim build/JSBSim) || die "Cannot find compiled JSBSim executable. Refusing cleanup."
msg "ArduPilot binary: $APBIN"
msg "JSBSim binary:   $JSBIN"
msg "Mode: $MODE"

# Basic dynamically-linked-library availability (not a complete runtime test).
if command -v ldd >/dev/null; then
 for bin in "$APBIN" "$JSBIN"; do
  if ldd "$bin" 2>/dev/null | grep -q 'not found'; then
    ldd "$bin" >&2 || true
    die "Missing shared library needed by $bin"
  fi
 done
fi

# Only known source-code suffixes are removed. Python, scripting, model, and
# configuration assets remain in place; preserving directories avoids broken
# relative paths. Skip .git and generated caches; handle objects separately.
source_filter=( -type f \( -iname '*.c' -o -iname '*.cc' -o -iname '*.cpp' -o -iname '*.cxx' -o -iname '*.h' -o -iname '*.hh' -o -iname '*.hpp' -o -iname '*.hxx' -o -iname '*.inl' -o -iname '*.ipp' \) )
add_targets() {
 local root=$1 f
 while IFS= read -r -d '' f; do
    [[ $f == "$APBIN" || $f == "$JSBIN" ]] && continue
    [[ -L $f ]] && continue
    TARGETS+=("$f")
 done < <(find "$root" \( -type d \( -name .git -o -name .venv -o -name venv -o -name venv-ardupilot-debian13 -o -name node_modules -o -name .tox \) -prune \) -o \( "${source_filter[@]}" -print0 \))
 # Compiler intermediates in build trees ONLY. Preserve .so, .elf,
 # executables, and any resources the runtime might reference.
 if [[ -d $root/build ]]; then
  while IFS= read -r -d '' f; do TARGETS+=("$f"); done < <(
    find "$root/build" -type f \( -name '*.o' -o -name '*.obj' -o -name '*.a' -o -name '*.lo' -o -name '*.gcda' -o -name '*.gcno' -o -name '*.d' \) -print0)
 fi
}
add_targets "$AP"
add_targets "$JS"

# Remove only explicit development directories, never the project roots.
# Prune matched directories so nested matches aren't counted or deleted twice.
add_dev_dirs() {
 local root=$1 d
 while IFS= read -r -d '' d; do
   [[ ! -L $d && $d != "$AP" && $d != "$JS" ]] || continue
   # Do not delete any directory containing either required binary.
   case "$APBIN/" in "$d/"*) die "Protected binary inside candidate directory: $d";; esac
   case "$JSBIN/" in "$d/"*) die "Protected binary inside candidate directory: $d";; esac
   DIR_TARGETS+=("$d")
 done < <(find "$root" -type d    \( -name .git -o -name .vscode -o -name venv -o -name .venv -o -name venv-ardupilot-debian13 \)    -print0 -prune)
}
add_dev_dirs "$AP"
add_dev_dirs "$JS"

if [[ -z $LOG ]]; then LOG="$BASE/flightstack_cleanup_$(date +%Y%m%d_%H%M%S)_${MODE}.log"; fi
[[ ! -e $LOG ]] || die "Log file already exists: $LOG"
# Never place log in a file planned for deletion.
for f in "${TARGETS[@]}"; do [[ $f != "$LOG" ]] || die "Log collides with cleanup"; done
# A report must not be written within a directory scheduled for removal.
LOG_DIR=$(dirname -- "$LOG")
LOG_DIR=$(realpath -m -- "$LOG_DIR")
for d in "${DIR_TARGETS[@]}"; do
 case "$LOG/" in "$d/"*) die "Log is inside a cleanup directory: $d";; esac
 case "$LOG_DIR/" in "$d/"*) die "Log directory scheduled for cleanup: $d";; esac
done
{
 printf 'FlightStack cleanup %s\nmode=%s\nAP=%s\nJS=%s\nAPBIN=%s\nJSBIN=%s\n' "$(date -Is)" "$MODE" "$AP" "$JS" "$APBIN" "$JSBIN"
 for f in "${TARGETS[@]}"; do
  [[ -f $f && ! -L $f ]] || continue
  printf '%q\n' "$f"
  ((COUNT+=1))
  size=$(stat -c %s -- "$f" 2>/dev/null || printf '0')
  ((BYTES+=size))
 done
 for d in "${DIR_TARGETS[@]}"; do
   [[ -d $d && ! -L $d ]] || continue
   printf 'DELETE_DIRECTORY %q\n' "$d"
   ((DIR_COUNT+=1))
   bytes=$(du -sk -- "$d" | awk '{print $1}')
   ((DIR_BYTES+=bytes*1024))
 done
 printf 'file_count=%d\nfile_size_bytes=%d\ndirectory_count=%d\ndirectory_size_approx_bytes=%d\n' "$COUNT" "$BYTES" "$DIR_COUNT" "$DIR_BYTES"
} > "$LOG"
msg "Candidate files: $COUNT; dev directories: $DIR_COUNT"
msg "Approx. file size: $(numfmt --to=iec --suffix=B "$BYTES")"
msg "Approx. dev-directory size: $(numfmt --to=iec --suffix=B "$DIR_BYTES")"
msg "Manifest: $LOG"
if [[ $MODE == dry-run ]]; then
 msg 'Nothing deleted. Review the manifest, then rerun with --apply.'
 exit 0
fi

# Automatic deletion, restricted to the manifest assembled above.
removed=0
failed=0
for f in "${TARGETS[@]}"; do
 [[ -f $f && ! -L $f ]] || continue
 if rm -- "$f"; then ((removed+=1)); else ((failed+=1)); fi
done
dir_removed=0
for d in "${DIR_TARGETS[@]}"; do
 [[ -d $d && ! -L $d ]] || continue
 if rm -rf -- "$d"; then ((dir_removed+=1)); else ((failed+=1)); fi
done
msg "Removed: $removed files and $dir_removed dev directories; failures: $failed"
# Verify binaries were not touched.
[[ -x $APBIN && -x $JSBIN ]] || die "Post-check: binary missing"
if ((failed)); then exit 2; fi
msg 'Source cleanup complete. WARNING: binary presence does not prove full SITL/VTOL runtime correctness.'
msg 'Run your real FlightStack integration tests before deleting your source backup.'

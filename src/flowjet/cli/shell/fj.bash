# bash completion for fj — multi-word natural-language queries.
# Install: eval "$(fj completion bash)"
# Requires: fj on PATH (or invoke via absolute path / active venv).

_fj() {
  local cur="${COMP_WORDS[COMP_CWORD]}"
  local fj_bin="${COMP_WORDS[0]}"
  local -a words=("${COMP_WORDS[@]:1}")
  local out
  local -a replies query
  local i=0 w

  out="$("$fj_bin" __complete -- "${words[@]}" 2>/dev/null)"
  [[ -z "$out" ]] && return 0

  while (( i < ${#words[@]} )); do
    w="${words[i]}"
    if [[ "$w" == -- ]]; then
      query=("${words[@]:i+1}")
      break
    fi
    if [[ "$w" == -h || "$w" == --help || "$w" == -V || "$w" == --version \
       || "$w" == --no-stream || "$w" == -v || "$w" == --verbose \
       || "$w" == -l || "$w" == --list \
       || "$w" == -f || "$w" == --follow ]]; then
      (( ++i ))
      continue
    fi
    if [[ "$w" == -c || "$w" == --config || "$w" == -t || "$w" == --thread \
       || "$w" == -w || "$w" == --workspace || "$w" == -n ]]; then
      (( ++i ))
      (( i < ${#words[@]} )) && (( ++i ))
      continue
    fi
    if [[ "$w" == --config=* || "$w" == --thread=* || "$w" == --workspace=* ]]; then
      (( ++i ))
      continue
    fi
    query=("${words[@]:i}")
    break
  done

  mapfile -t replies <<< "$out"
  COMPREPLY=()

  if [[ ${#query[@]} -eq 0 || "${query[0]}" == -* \
     || "${query[0]}" == setup || "${query[0]}" == doctor \
     || "${query[0]}" == doctor-fix || "${query[0]}" == serve \
     || "${query[0]}" == completion ]]; then
    local r
    for r in "${replies[@]}"; do
      [[ -z "$r" ]] && continue
      if [[ -z "$cur" || "$r" == "$cur"* ]]; then
        COMPREPLY+=("$r")
      fi
    done
    return 0
  fi

  local r
  for r in "${replies[@]}"; do
    [[ -z "$r" ]] && continue
    printf -v r '%q' "$r"
    COMPREPLY+=("$r")
  done
  return 0
}

complete -F _fj -o nospace fj

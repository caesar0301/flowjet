#compdef fj
# zsh completion for fj — multi-word natural-language queries.
# Install: eval "$(fj completion zsh)"
# Requires: fj on PATH (or invoke via absolute path / active venv).

_fj() {
  local -a replies opt_words query_words
  local i=2 word fj_bin
  local -a value_flags
  value_flags=(-c --config -t --thread -w --workspace -n)

  # Prefer the same binary the user is completing (venv path, alias target, etc.).
  fj_bin="${words[1]}"
  if [[ "$fj_bin" != /* && "$fj_bin" != ./* && "$fj_bin" != ../* ]]; then
    fj_bin="${commands[fj]:-fj}"
  fi

  while (( i <= $#words )); do
    word="${words[i]}"
    if [[ "$word" == -- ]]; then
      (( i++ ))
      query_words=("${words[i,-1]}")
      break
    fi
    if [[ "$word" == -h || "$word" == --help || "$word" == -V || "$word" == --version \
       || "$word" == --no-stream || "$word" == -v || "$word" == --verbose \
       || "$word" == -l || "$word" == --list \
       || "$word" == -f || "$word" == --follow ]]; then
      opt_words+=("$word")
      (( i++ ))
      continue
    fi
    if (( ${value_flags[(Ie)$word]} )); then
      opt_words+=("$word")
      (( i++ ))
      if (( i <= $#words )); then
        opt_words+=("${words[i]}")
        (( i++ ))
      fi
      continue
    fi
    if [[ "$word" == --config=* || "$word" == --thread=* || "$word" == --workspace=* ]]; then
      opt_words+=("$word")
      (( i++ ))
      continue
    fi
    query_words=("${words[i,-1]}")
    break
  done

  # Local-first for snappy Tab; full LLM path is still available via engine timeout.
  replies=("${(@f)$("$fj_bin" __complete -- "${words[@]:1}" 2>/dev/null)}")
  (( ${#replies} )) || return 1

  if [[ ${#query_words} -eq 0 || "${query_words[1]}" == -* \
     || "${query_words[1]}" == setup || "${query_words[1]}" == completion \
     || "${query_words[1]}" == doctor || "${query_words[1]}" == doctor-fix \
     || "${query_words[1]}" == serve \
     || ( ${#query_words} -eq 1 && ( setup == ${query_words[1]}* || completion == ${query_words[1]}* \
        || serve == ${query_words[1]}* ) ) ]]; then
    compadd -Q -- "${replies[@]}"
    return
  fi

  local prefix="${(j: :)query_words}"
  words=("${words[1]}" "${opt_words[@]}" "$prefix")
  CURRENT=$(( ${#opt_words} + 2 ))
  PREFIX="$prefix"
  if (( ${#opt_words} )); then
    IPREFIX="${words[1]} ${(j: :)opt_words} "
  else
    IPREFIX="${words[1]} "
  fi
  compadd -U -Q -- "${replies[@]}"
}

compdef _fj fj

# =============================================================================
## CodeAgent CLI Wrapper
# =============================================================================
codeagent() {
    local script_path="/home/willi/ai-dev-base/scripts/dev.sh"
    local container_prefix="ai-dev-base-dev-run-"
    
    # Spezialfall: "enter" - Neue Shell im laufenden Container
    if [[ "$1" == "enter" ]]; then
        local container_id=$(docker ps -q --filter "name=${container_prefix}" | head -n 1)
        if [ -z "$container_id" ]; then
            echo "Fehler: Kein laufender Container mit Praefix '${container_prefix}' gefunden."
            return 1
        fi
        echo "Oeffne neue Zsh-Session in Container: $container_id"
        docker exec -it "$container_id" zsh
        return 0
    fi
    
    # Spezialfall: "here" als Shortcut fuer "start --here"
    if [[ "$1" == "here" ]]; then
        shift
        "$script_path" start --here "$@"
        return $?
    fi
    
    # Standardverhalten: Pruefen, ob das Skript existiert und ausfuehren
    if [[ ! -f "$script_path" ]]; then
        echo "Fehler: CodeAgent Skript nicht gefunden unter:"
        echo "   $script_path"
        return 1
    fi
    "$script_path" "$@"
}

# Vervollstaendigung
_codeagent_completion() {
    local curcontext="$curcontext" state line
    typeset -A opt_args
    
    _arguments -C \
        '1:command:->commands' \
        '*:option:->options'
    
    case $state in
        commands)
            local -a commands
            commands=(
                'build:Baut das Base-Image'
                'start:Startet Container (dediziertes Netzwerk)'
                'run:Fuehrt Agent headless aus (ephemer, non-interaktiv)'
                'auth:Startet Container fuer OAuth (Host-Netzwerk)'
                'status:Zeigt Container, Volumes und MCP-Status'
                'clean:Verwaltet Container und Volumes'
                'audit:Zeigt Docker Proxy Audit-Log'
                'enter:Oeffnet neue Zsh-Session im laufenden Container'
                'here:Shortcut fuer "start --here" (mountet pwd)'
                'update:Aktualisiert CLI Agent Versionen im Dockerfile'
                'help:Zeigt Hilfe'
            )
            _describe 'command' commands
            ;;
        options)
            case $line[1] in
                start|here)
                    local -a opts
                    opts=(
                        '--docker:Docker-Zugriff via Proxy aktivieren'
                        '--firewall:Netzwerk-Firewall aktivieren'
                        '--here:Aktuelles Verzeichnis unter ~/workspace/ mounten'
                        '--mount:Beliebigen Pfad unter ~/workspace/ mounten'
                    )
                    _describe 'option' opts
                    
                    # Pfadvervollstaendigung nach --mount
                    if [[ "${line[-1]}" == "--mount" ]]; then
                        _files -/
                    fi
                    ;;
                run)
                    # Erstes Argument nach "run" ist der Agent
                    if [[ ${#line[@]} -eq 2 ]]; then
                        local -a agents
                        agents=(
                            'claude:Anthropic Claude Code'
                            'gemini:Google Gemini CLI'
                            'codex:OpenAI Codex CLI'
                            'opencode:OpenCode CLI'
                        )
                        _describe 'agent' agents
                    else
                        local -a opts
                        opts=(
                            '--write:Agent darf Dateien aendern'
                            '--json:Maschinenlesbarer JSON-Output'
                            '--model:Spezifisches Modell waehlen'
                            '--docker:Docker-Zugriff via Proxy aktivieren'
                            '--firewall:Netzwerk-Firewall aktivieren'
                            '--mount:Alternativer Workspace-Pfad'
                        )
                        _describe 'option' opts
                    fi
                    ;;
                auth)
                    local -a opts
                    opts=('--docker:Docker-Zugriff via Proxy aktivieren')
                    _describe 'option' opts
                    ;;
                clean)
                    # Erstes Argument nach "clean"
                    if [[ ${#line[@]} -eq 2 ]]; then
                        local -a subcmds
                        subcmds=(
                            'volumes:Verwaltet Volumes (list, delete)'
                            '--all:Entfernt ALLES (Container, Volumes, Netzwerk)'
                        )
                        _describe 'subcommand' subcmds
                    elif [[ "${line[2]}" == "volumes" ]]; then
                        local -a volopts
                        volopts=(
                            '--credentials:Loescht Auth-Tokens (claude, gemini, etc.)'
                            '--tools:Loescht Tool-Configs (azure, pulumi)'
                            '--cache:Loescht Caches (uv, tools)'
                            '--data:Loescht App-Daten (opencode)'
                        )
                        _describe 'volume-option' volopts
                    fi
                    ;;
            esac
            ;;
    esac
}
compdef _codeagent_completion codeagent

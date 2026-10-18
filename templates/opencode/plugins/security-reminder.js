const PATTERNS = [
  {
    name: "github_actions_workflow",
    path: (p) => p.includes(".github/workflows/") && /\.ya?ml$/.test(p),
    message:
      "Security warning: GitHub Actions workflow edits can introduce command injection. Put untrusted event fields into env vars and quote shell expansions.",
  },
  {
    name: "child_process_exec",
    snippets: ["child_process.exec", "exec(", "execSync("],
    message:
      "Security warning: shell exec patterns can introduce command injection. Prefer argument-vector APIs such as execFile/spawn without a shell.",
  },
  {
    name: "new_function",
    snippets: ["new Function"],
    message:
      "Security warning: new Function() evaluates dynamic code and can lead to code injection.",
  },
  {
    name: "eval",
    snippets: ["eval("],
    message:
      "Security warning: eval() executes arbitrary code. Prefer structured parsing or explicit dispatch.",
  },
  {
    name: "dangerously_set_html",
    snippets: ["dangerouslySetInnerHTML"],
    message:
      "Security warning: dangerouslySetInnerHTML can lead to XSS unless content is sanitized.",
  },
  {
    name: "document_write",
    snippets: ["document.write"],
    message:
      "Security warning: document.write() is XSS-prone and should be avoided.",
  },
  {
    name: "inner_html",
    snippets: [".innerHTML =", ".innerHTML="],
    message:
      "Security warning: assigning innerHTML can lead to XSS. Prefer textContent or sanitized HTML.",
  },
  {
    name: "pickle",
    snippets: ["pickle"],
    message: "Security warning: Python pickle is unsafe for untrusted data.",
  },
  {
    name: "os_system",
    snippets: ["os.system", "from os import system"],
    message:
      "Security warning: os.system should not be used with user-controlled input.",
  },
];

function splitCommand(command) {
  try {
    return (
      command
        .match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g)
        ?.map((token) => token.replace(/^['"]|['"]$/g, "")) || []
    );
  } catch {
    return command.split(/\s+/).filter(Boolean);
  }
}

function unsafeRmTarget(token) {
  return (
    token === "/" ||
    token === "/*" ||
    token === "~" ||
    token === "$HOME" ||
    token === "${HOME}" ||
    token === "$PWD" ||
    token === "${PWD}" ||
    token === "." ||
    token === "./" ||
    token.startsWith("~/") ||
    token.startsWith("$HOME/") ||
    token.startsWith("${HOME}/") ||
    token.startsWith("$PWD/") ||
    token.startsWith("${PWD}/") ||
    token.startsWith("./")
  );
}

function gitSubcommand(tokens, start) {
  let index = start;
  while (index < tokens.length) {
    const token = tokens[index];
    if (token === "--")
      return { subcommand: tokens[index + 1], args: tokens.slice(index + 2) };
    if (["-C", "-c", "--git-dir", "--work-tree"].includes(token)) {
      index += 2;
      continue;
    }
    if (
      token.startsWith("-C") ||
      token.startsWith("--git-dir=") ||
      token.startsWith("--work-tree=") ||
      token.startsWith("-c")
    ) {
      index += 1;
      continue;
    }
    if (token.startsWith("-")) {
      index += 1;
      continue;
    }
    return { subcommand: token, args: tokens.slice(index + 1) };
  }
  return { subcommand: "", args: [] };
}

function forcePushArg(arg) {
  return (
    arg === "-f" ||
    arg === "--force" ||
    arg === "--force-with-lease" ||
    arg.startsWith("--force=") ||
    arg.startsWith("--force-with-lease=") ||
    arg.startsWith("+")
  );
}

function chmod777(tokens, start) {
  for (const arg of tokens.slice(start)) {
    if (arg === "--") continue;
    if (arg.startsWith("-") && arg !== "-") continue;
    if (/^[0-7]*777$/.test(arg)) return true;
  }
  return false;
}

function deniedShellReason(command) {
  const tokens = splitCommand(command);
  if (tokens.includes("sudo")) return "sudo is not allowed by OpenCode policy.";
  if (/curl\b.*\|\s*(bash|sh)\b/.test(command))
    return "Piping curl directly into a shell is not allowed.";
  if (/\bchmod\s+777\b/.test(command)) return "chmod 777 is not allowed.";

  for (let index = 0; index < tokens.length; index++) {
    if (tokens[index] === "git") {
      const { subcommand, args } = gitSubcommand(tokens, index + 1);
      if (subcommand === "push" && args.some(forcePushArg)) {
        return "Force-push is not allowed by OpenCode policy.";
      }
      if (subcommand === "reset" && args.includes("--hard")) {
        return "git reset --hard is not allowed.";
      }
    }
    if (tokens[index] === "chmod" && chmod777(tokens, index + 1)) {
      return "chmod 777 is not allowed.";
    }
    if (tokens[index] === "rm") {
      let recursive = false;
      let force = false;
      for (const arg of tokens.slice(index + 1)) {
        if (arg.startsWith("-") && arg !== "-") {
          const flags = arg.replace(/^-+/, "");
          recursive ||=
            flags.includes("r") || flags.includes("R") || flags === "recursive";
          force ||= flags.includes("f") || flags === "force";
          continue;
        }
        if (recursive && force && unsafeRmTarget(arg)) {
          return "Broad recursive forced rm against root, home, or cwd is not allowed.";
        }
      }
    }
  }
  return "";
}

function contentFrom(tool, args) {
  if (!args) return "";
  if (tool === "write") return String(args.content || "");
  if (tool === "edit") return String(args.newString || "");
  if (tool === "patch") return String(args.patchText || "");
  return "";
}

export default {
  id: "djinn.security-reminder",
  async setup(ctx) {
    await ctx.tool.hook("execute.before", async (event) => {
      const tool = event.tool;
      const args = event.input || {};
      if (tool === "shell") {
        const command = String(args.command || "");
        const reason = deniedShellReason(command);
        if (reason) throw new Error(reason);
        return;
      }
      if (!["write", "edit", "patch"].includes(tool)) return;
      const path = String(args.path || "");
      const content = contentFrom(tool, args);
      for (const pattern of PATTERNS) {
        if (
          pattern.path?.(path) ||
          pattern.snippets?.some((snippet) => content.includes(snippet))
        ) {
          console.warn(`[opencode] ${pattern.message}`);
          return;
        }
      }
    });
  },
};

import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);

export default {
  id: "djinn.session-start-status",
  setup(ctx) {
    const controller = new AbortController();
    const events = (async () => {
      try {
        for await (const event of ctx.event.subscribe({
          signal: controller.signal,
        })) {
          if (event.type !== "session.created") continue;
          try {
            const { stdout } = await run("git", ["status", "--porcelain"], {
              cwd: event.data.location.directory,
              signal: controller.signal,
            });
            if (stdout.trim()) {
              console.warn(
                "[opencode] Session status: uncommitted changes in current repo",
              );
            }
          } catch (error) {
            if (!controller.signal.aborted && error.code !== 128) {
              console.warn("[opencode] Session status: Git inspection failed");
            }
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) {
          console.warn(
            "[opencode] Session status: event subscription failed",
            error,
          );
        }
      }
    })();
    return async () => {
      controller.abort();
      await events;
    };
  },
};

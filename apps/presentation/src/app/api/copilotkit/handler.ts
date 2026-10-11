import { HttpAgent } from "@ag-ui/client";
import { CopilotRuntime, createCopilotRuntimeHandler } from "@copilotkit/runtime/v2";

const agentUrl = process.env.SOT_AGENT_URL ?? "http://127.0.0.1:8000/ag-ui";

const runtime = new CopilotRuntime({
  agents: {
    default: new HttpAgent({ url: agentUrl }),
  },
  forwardHeaders: {
    allow: ["authorization", "x-sot-user"],
  },
});

export const handler = createCopilotRuntimeHandler({
  runtime,
  basePath: "/api/copilotkit",
});

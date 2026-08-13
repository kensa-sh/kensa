import { z } from "zod";

import { KensaCoreError, parseInput } from "./errors.js";
import { canonicalJson, jsonValueSchema, type JsonValue } from "./json.js";

const sourceSchema = z.enum(["agent", "simulator"]);
const terminationSourceSchema = z.enum(["agent", "simulator", "engine"]);
const nonblankStringSchema = z
  .string()
  .refine((value) => value.trim().length > 0, {
    message: "expected string to contain a non-whitespace character",
  });

const functionCallSchema = z
  .strictObject({
    name: nonblankStringSchema,
    arguments: z.string(),
  })
  .superRefine((call, context) => {
    try {
      const parsed: unknown = JSON.parse(call.arguments);
      if (!isPlainObject(parsed)) {
        addIssue(
          context,
          ["arguments"],
          "tool function arguments must encode a JSON object",
        );
      }
    } catch {
      addIssue(
        context,
        ["arguments"],
        "tool function arguments must be valid JSON",
      );
    }
  });

const toolCallSchema = z.strictObject({
  id: nonblankStringSchema,
  type: z.literal("function"),
  function: functionCallSchema,
});

const namedTextMessageSchema = z.strictObject({
  role: z.enum(["system", "developer", "user"]),
  content: z.string(),
  name: z.string().optional(),
});

const assistantMessageSchema = z
  .strictObject({
    role: z.literal("assistant"),
    content: z.string().nullable().optional(),
    name: z.string().optional(),
    tool_calls: z.array(toolCallSchema).min(1).optional(),
  })
  .superRefine((message, context) => {
    if (
      message.tool_calls === undefined &&
      typeof message.content !== "string"
    ) {
      addIssue(
        context,
        ["content"],
        "assistant messages require string content unless tool calls are present",
      );
    }
    if (message.tool_calls !== undefined) {
      const ids = new Set<string>();
      for (const [index, call] of message.tool_calls.entries()) {
        if (ids.has(call.id)) {
          addIssue(
            context,
            ["tool_calls", index, "id"],
            "assistant tool call IDs must be unique",
          );
        }
        ids.add(call.id);
      }
    }
  });

const toolMessageSchema = z.strictObject({
  role: z.literal("tool"),
  tool_call_id: nonblankStringSchema,
  content: z.string(),
});

const messageSchema = z.union([
  namedTextMessageSchema,
  assistantMessageSchema,
  toolMessageSchema,
]);

const messagesSchema = z
  .array(messageSchema)
  .superRefine((messages, context) => {
    const pending = new Set<string>();
    for (const [index, message] of messages.entries()) {
      if (message.role === "tool") {
        if (!pending.delete(message.tool_call_id)) {
          addIssue(
            context,
            [index, "tool_call_id"],
            "tool message references an unknown tool call",
          );
        }
        continue;
      }
      if (pending.size > 0) {
        addIssue(
          context,
          [index],
          "assistant tool calls must be followed by matching tool messages",
        );
        pending.clear();
      }
      if (message.role === "assistant" && message.tool_calls !== undefined) {
        for (const call of message.tool_calls) {
          pending.add(call.id);
        }
      }
    }
    if (pending.size > 0) {
      addIssue(
        context,
        [messages.length],
        "assistant tool calls must be followed by matching tool messages",
      );
    }
  });

const startSchema = z
  .strictObject({
    messages: messagesSchema,
    mode: z.enum(["direct", "simulated"]),
    max_agent_responses: z.number().int().positive().nullable(),
    starts_with: sourceSchema,
  })
  .superRefine((input, context) => {
    if (input.mode === "direct") {
      if (input.max_agent_responses !== null) {
        addIssue(
          context,
          ["max_agent_responses"],
          "direct conversations cannot set a response bound",
        );
      }
      if (input.starts_with !== "agent") {
        addIssue(
          context,
          ["starts_with"],
          "direct conversations must start with the agent",
        );
      }
    } else if (input.max_agent_responses === null) {
      addIssue(
        context,
        ["max_agent_responses"],
        "simulated conversations require a response bound",
      );
    }
  });

const responseSchema = z
  .strictObject({
    source: sourceSchema,
    content: nonblankStringSchema.nullable(),
    output: jsonValueSchema.nullable(),
    output_recorded: z.boolean(),
    termination_reason: nonblankStringSchema.nullable(),
  })
  .superRefine((response, context) => {
    if (!response.output_recorded && response.output !== null) {
      addIssue(
        context,
        ["output"],
        "unrecorded output must use the null wire representation",
      );
    }
    if (response.source === "simulator" && response.output_recorded) {
      addIssue(
        context,
        ["output_recorded"],
        "simulator responses cannot record output",
      );
    }
  });

const actionSchema = z.strictObject({
  source: sourceSchema,
  messages: messagesSchema,
  response_index: z.number().int().positive(),
  agent_responses: z.number().int().nonnegative(),
});

const terminationSchema = z.strictObject({
  source: terminationSourceSchema,
  reason: nonblankStringSchema,
});

const resultSchema = z
  .strictObject({
    phase: z.literal("complete"),
    messages: messagesSchema,
    output: jsonValueSchema.nullable(),
    output_recorded: z.boolean(),
    termination: terminationSchema,
  })
  .superRefine((result, context) => {
    if (!result.output_recorded && result.output !== null) {
      addIssue(
        context,
        ["output"],
        "unrecorded output must use the null wire representation",
      );
    }
  });

export type ConversationSource = z.infer<typeof sourceSchema>;
export type ConversationMessage = z.infer<typeof messageSchema>;
export type ConversationStart = z.infer<typeof startSchema>;
export type ConversationResponseObservation = z.infer<typeof responseSchema>;
export type ConversationAction = z.infer<typeof actionSchema>;
export type ConversationTermination = z.infer<typeof terminationSchema>;
export type ConversationComplete = z.infer<typeof resultSchema>;

export interface ConversationAwaitingResponse {
  phase: "awaiting_response";
  mode: ConversationStart["mode"];
  messages: ConversationMessage[];
  output: JsonValue | null;
  output_recorded: boolean;
  source: ConversationSource;
  max_agent_responses: number | null;
  response_index: number;
  agent_responses: number;
}

export type ConversationState =
  ConversationAwaitingResponse | ConversationComplete;

export class ConversationTransitionError extends KensaCoreError {
  constructor(message: string) {
    super("invalid_transition", message);
    this.name = "ConversationTransitionError";
  }
}

export function parseConversationMessages(
  input: unknown,
): ConversationMessage[] {
  return parseInput(
    messagesSchema,
    input,
    "conversation messages violate the core contract",
  );
}

export function parseConversationAction(input: unknown): ConversationAction {
  return parseInput(
    actionSchema,
    input,
    "conversation action violates the core contract",
  );
}

export function parseConversationResult(input: unknown): ConversationComplete {
  return parseInput(
    resultSchema,
    input,
    "conversation result violates the core contract",
  );
}

export function startConversation(
  input: unknown,
): ConversationAwaitingResponse {
  const start = parseInput(
    startSchema,
    input,
    "conversation start violates the core contract",
  );
  return {
    phase: "awaiting_response",
    mode: start.mode,
    messages: start.messages,
    output: null,
    output_recorded: false,
    source: start.starts_with,
    max_agent_responses: start.max_agent_responses,
    response_index: 1,
    agent_responses: 0,
  };
}

export function conversationAction(
  state: ConversationState,
): ConversationAction | null {
  if (state.phase === "complete") {
    return null;
  }
  const messages =
    state.source === "agent"
      ? cloneMessages(state.messages)
      : simulatorHistory(state.messages);
  return parseConversationAction({
    source: state.source,
    messages,
    response_index: state.response_index,
    agent_responses: state.agent_responses,
  });
}

export function observeConversation(
  state: ConversationAwaitingResponse,
  input: unknown,
): ConversationState;
export function observeConversation(
  state: ConversationState,
  input: unknown,
): ConversationState;
export function observeConversation(
  state: ConversationState,
  input: unknown,
): ConversationState {
  if (state.phase !== "awaiting_response") {
    throw new ConversationTransitionError(
      `cannot observe conversation in ${state.phase} phase`,
    );
  }
  const response = parseInput(
    responseSchema,
    input,
    "conversation response violates the core contract",
  );
  if (response.source !== state.source) {
    throw new ConversationTransitionError(
      `expected ${state.source} response, received ${response.source}`,
    );
  }
  if (
    state.mode === "simulated" &&
    response.content === null &&
    response.termination_reason === null
  ) {
    throw new ConversationTransitionError(
      "non-terminal simulated responses require content",
    );
  }

  const messages = appendResponse(state.messages, response);
  const output = outputAfter(state, response);
  const outputRecorded =
    state.output_recorded ||
    (response.source === "agent" &&
      (response.output_recorded || response.content !== null));
  const agentResponses =
    state.agent_responses + (response.source === "agent" ? 1 : 0);
  const termination = terminationAfter(state, response, agentResponses);
  if (termination !== null) {
    return parseConversationResult({
      phase: "complete",
      messages,
      output,
      output_recorded: outputRecorded,
      termination,
    });
  }
  return {
    ...state,
    messages,
    output,
    output_recorded: outputRecorded,
    source: response.source === "agent" ? "simulator" : "agent",
    response_index: state.response_index + 1,
    agent_responses: agentResponses,
  };
}

function appendResponse(
  messages: ConversationMessage[],
  response: ConversationResponseObservation,
): ConversationMessage[] {
  if (response.content === null) {
    return cloneMessages(messages);
  }
  return parseConversationMessages([
    ...messages,
    {
      role: response.source === "agent" ? "assistant" : "user",
      content: response.content,
    },
  ]);
}

function outputAfter(
  state: ConversationAwaitingResponse,
  response: ConversationResponseObservation,
): JsonValue | null {
  if (response.source !== "agent") {
    return cloneJson(state.output);
  }
  if (response.output_recorded) {
    return cloneJson(response.output);
  }
  return response.content === null ? cloneJson(state.output) : response.content;
}

function terminationAfter(
  state: ConversationAwaitingResponse,
  response: ConversationResponseObservation,
  agentResponses: number,
): ConversationTermination | null {
  if (response.termination_reason !== null) {
    return {
      source: response.source,
      reason: response.termination_reason,
    };
  }
  if (state.mode === "direct") {
    return { source: "engine", reason: "direct" };
  }
  if (
    response.source === "agent" &&
    agentResponses === state.max_agent_responses
  ) {
    return { source: "engine", reason: "max_turns" };
  }
  return null;
}

function simulatorHistory(
  messages: ConversationMessage[],
): ConversationMessage[] {
  const projected = messages.flatMap((message) => {
    if (message.role !== "user" && message.role !== "assistant") {
      return [];
    }
    if (typeof message.content !== "string") {
      return [];
    }
    if (
      message.role === "assistant" &&
      message.tool_calls !== undefined &&
      message.content.trim().length === 0
    ) {
      return [];
    }
    return [
      {
        role: message.role,
        content: message.content,
        ...(message.name === undefined ? {} : { name: message.name }),
      },
    ];
  });
  return parseConversationMessages(projected);
}

function cloneMessages(messages: ConversationMessage[]): ConversationMessage[] {
  return parseConversationMessages(messages);
}

function cloneJson<T extends JsonValue>(value: T): T {
  return JSON.parse(canonicalJson(value)) as T;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function addIssue(
  context: z.RefinementCtx,
  path: (string | number)[],
  message: string,
): void {
  context.addIssue({ code: "custom", path, message });
}

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import {
  conversationAction,
  ConversationTransitionError,
  CoreValidationError,
  observeConversation,
  parseConversationAction,
  parseConversationMessages,
  parseConversationResult,
  startConversation,
  type ConversationAwaitingResponse,
  type ConversationMessage,
  type ConversationResponseObservation,
  type ConversationState,
} from "../src/index.js";

const unrecorded = {
  output: null,
  output_recorded: false,
  termination_reason: null,
} as const;

function direct(messages: unknown[] = []): ConversationAwaitingResponse {
  return startConversation({
    messages,
    mode: "direct",
    max_agent_responses: null,
    starts_with: "agent",
  });
}

function simulated(
  messages: unknown[] = [],
  options: { max?: number; startsWith?: "agent" | "simulator" } = {},
): ConversationAwaitingResponse {
  return startConversation({
    messages,
    mode: "simulated",
    max_agent_responses: options.max ?? 2,
    starts_with: options.startsWith ?? "simulator",
  });
}

function respond(
  state: ConversationAwaitingResponse,
  input: Partial<ConversationResponseObservation> = {},
): ConversationState {
  return observeConversation(state, {
    source: state.source,
    content: null,
    ...unrecorded,
    ...input,
  });
}

function awaiting(state: ConversationState): ConversationAwaitingResponse {
  if (state.phase !== "awaiting_response") {
    throw new Error("expected active conversation");
  }
  return state;
}

function expectCoreIssue(operation: () => unknown, message: string): void {
  try {
    operation();
  } catch (error) {
    expect(error).toBeInstanceOf(CoreValidationError);
    expect(
      (error as CoreValidationError).issues.map((issue) => issue.message),
    ).toContainEqual(expect.stringContaining(message));
    return;
  }
  throw new Error("expected core validation failure");
}

describe("conversation lifecycle", () => {
  it("matches the shared conversation lifecycle fixture", () => {
    const fixture = JSON.parse(
      readFileSync(
        new URL("../conformance/conversation.json", import.meta.url),
        "utf8",
      ),
    ) as {
      start: unknown;
      actions: unknown[];
      observations: unknown[];
      result: unknown;
    };
    let state: ConversationState = startConversation(fixture.start);

    for (const [index, observation] of fixture.observations.entries()) {
      expect(conversationAction(state)).toEqual(fixture.actions[index]);
      state = observeConversation(state, observation);
    }
    expect(state).toEqual(fixture.result);
    expect(parseConversationResult(fixture.result)).toEqual(fixture.result);
  });

  it.each([
    {
      response: { content: "hello" },
      messages: [{ role: "assistant", content: "hello" }],
      output: "hello",
      outputRecorded: true,
      source: "engine",
      reason: "direct",
    },
    {
      response: {
        content: "hello",
        output: { intent: "greet" },
        output_recorded: true,
      },
      messages: [{ role: "assistant", content: "hello" }],
      output: { intent: "greet" },
      outputRecorded: true,
      source: "engine",
      reason: "direct",
    },
    {
      response: { content: "hello", output_recorded: true },
      messages: [{ role: "assistant", content: "hello" }],
      output: null,
      outputRecorded: true,
      source: "engine",
      reason: "direct",
    },
    {
      response: { output: { status: "done" }, output_recorded: true },
      messages: [],
      output: { status: "done" },
      outputRecorded: true,
      source: "engine",
      reason: "direct",
    },
    {
      response: {},
      messages: [],
      output: null,
      outputRecorded: false,
      source: "engine",
      reason: "direct",
    },
    {
      response: { termination_reason: "finished" },
      messages: [],
      output: null,
      outputRecorded: false,
      source: "agent",
      reason: "finished",
    },
  ])(
    "derives direct response output and termination %#",
    ({ response, messages, output, outputRecorded, source, reason }) => {
      const result = respond(direct(), response);

      expect(result).toEqual({
        phase: "complete",
        messages,
        output,
        output_recorded: outputRecorded,
        termination: { source, reason },
      });
      expect(conversationAction(result)).toBeNull();
    },
  );

  it("preserves response content and termination reasons exactly", () => {
    const result = respond(direct(), {
      content: "  exact response  ",
      termination_reason: "  exact reason  ",
    });

    expect(result).toEqual({
      phase: "complete",
      messages: [{ role: "assistant", content: "  exact response  " }],
      output: "  exact response  ",
      output_recorded: true,
      termination: { source: "agent", reason: "  exact reason  " },
    });
  });

  it("alternates simulated responders and counts only accepted agent responses", () => {
    let state: ConversationState = simulated([], {
      startsWith: "agent",
      max: 2,
    });
    expect(conversationAction(state)).toEqual({
      source: "agent",
      messages: [],
      response_index: 1,
      agent_responses: 0,
    });

    state = respond(awaiting(state), { content: "a1" });
    expect(state).toMatchObject({
      phase: "awaiting_response",
      source: "simulator",
      response_index: 2,
      agent_responses: 1,
      output: "a1",
      output_recorded: true,
    });
    expect(conversationAction(state)).toEqual({
      source: "simulator",
      messages: [{ role: "assistant", content: "a1" }],
      response_index: 2,
      agent_responses: 1,
    });

    state = respond(awaiting(state), { content: "s1" });
    expect(state).toMatchObject({
      phase: "awaiting_response",
      source: "agent",
      response_index: 3,
      agent_responses: 1,
      output: "a1",
    });
    state = respond(awaiting(state), { content: "a2" });
    expect(state).toEqual({
      phase: "complete",
      messages: [
        { role: "assistant", content: "a1" },
        { role: "user", content: "s1" },
        { role: "assistant", content: "a2" },
      ],
      output: "a2",
      output_recorded: true,
      termination: { source: "engine", reason: "max_turns" },
    });
  });

  it("prefers explicit responder termination over the agent response bound", () => {
    const agentEnd = respond(simulated([], { startsWith: "agent", max: 1 }), {
      content: "final",
      termination_reason: "resolved",
    });
    expect(agentEnd).toMatchObject({
      phase: "complete",
      termination: { source: "agent", reason: "resolved" },
    });

    const simulatorEnd = respond(simulated([], { max: 3 }), {
      content: "goodbye",
      termination_reason: "done",
    });
    expect(simulatorEnd).toEqual({
      phase: "complete",
      messages: [{ role: "user", content: "goodbye" }],
      output: null,
      output_recorded: false,
      termination: { source: "simulator", reason: "done" },
    });
  });

  it("projects exact isolated histories for each responder", () => {
    const initial: ConversationMessage[] = [
      { role: "system", content: "private system" },
      { role: "developer", content: "private developer" },
      { role: "user", content: "" },
      { role: "assistant", content: "" },
      { role: "user", content: "hello", name: "customer" },
      {
        role: "assistant",
        content: "checking",
        name: "support",
        tool_calls: [
          {
            id: "call_1",
            type: "function",
            function: { name: "lookup", arguments: "{}" },
          },
        ],
      },
      { role: "tool", tool_call_id: "call_1", content: "private result" },
      {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: "call_2",
            type: "function",
            function: { name: "private_lookup", arguments: "{}" },
          },
        ],
      },
      { role: "tool", tool_call_id: "call_2", content: "more private data" },
      {
        role: "assistant",
        content: "",
        tool_calls: [
          {
            id: "call_3",
            type: "function",
            function: { name: "silent_lookup", arguments: "{}" },
          },
        ],
      },
      { role: "tool", tool_call_id: "call_3", content: "silent private data" },
      { role: "assistant", content: "found it" },
    ];
    const state = simulated(initial);
    const simulatorAction = conversationAction(state)!;

    expect(simulatorAction.messages).toEqual([
      { role: "user", content: "" },
      { role: "assistant", content: "" },
      { role: "user", content: "hello", name: "customer" },
      { role: "assistant", content: "checking", name: "support" },
      { role: "assistant", content: "found it" },
    ]);
    simulatorAction.messages[0]!.content = "mutated";

    const afterSimulator = respond(state, { content: "follow-up" });
    expect(conversationAction(afterSimulator)!.messages).toEqual([
      ...initial,
      { role: "user", content: "follow-up" },
    ]);
    expect(state.messages[2]!.content).toBe("");
  });

  it("does not alias accepted response output or returned terminal values", () => {
    const output = { nested: { values: [1] } };
    const result = respond(direct(), {
      content: "done",
      output,
      output_recorded: true,
    });
    output.nested.values.push(2);
    expect(result.output).toEqual({ nested: { values: [1] } });
    if (result.phase !== "complete" || typeof result.output !== "object") {
      throw new Error("expected object output");
    }
    (result.output as { nested: { values: number[] } }).nested.values.push(3);
    expect(output).toEqual({ nested: { values: [1, 2] } });
  });

  it.each([
    {
      value: {
        messages: [],
        mode: "direct",
        max_agent_responses: 1,
        starts_with: "agent",
      },
      message: "response bound",
    },
    {
      value: {
        messages: [],
        mode: "direct",
        max_agent_responses: null,
        starts_with: "simulator",
      },
      message: "must start",
    },
    {
      value: {
        messages: [],
        mode: "simulated",
        max_agent_responses: null,
        starts_with: "agent",
      },
      message: "require a response bound",
    },
    {
      value: {
        messages: [],
        mode: "simulated",
        max_agent_responses: 0,
        starts_with: "agent",
      },
      message: "expected number to be >0",
    },
    {
      value: {
        messages: [],
        mode: "simulated",
        max_agent_responses: true,
        starts_with: "agent",
      },
      message: "number",
    },
  ])("rejects invalid start configuration %#", ({ value, message }) => {
    expectCoreIssue(() => startConversation(value), message);
  });

  it.each([
    {
      response: { source: "simulator", content: "wrong" },
      error: ConversationTransitionError,
      message: "expected agent",
    },
    {
      response: { content: " " },
      error: CoreValidationError,
      message: "expected string",
    },
    {
      response: { termination_reason: " " },
      error: CoreValidationError,
      message: "expected string",
    },
    {
      response: { output: "hidden" },
      error: CoreValidationError,
      message: "unrecorded output",
    },
    {
      response: {
        source: "simulator",
        content: "hello",
        output_recorded: true,
      },
      error: CoreValidationError,
      message: "cannot record output",
    },
  ])(
    "rejects invalid responses without mutating state %#",
    ({ response, error, message }) => {
      const state = direct([{ role: "user", content: "initial" }]);
      const before = structuredClone(state);
      expect(() =>
        respond(state, response as Partial<ConversationResponseObservation>),
      ).toThrow(error);
      if (error === CoreValidationError) {
        expectCoreIssue(
          () =>
            respond(
              state,
              response as Partial<ConversationResponseObservation>,
            ),
          message,
        );
      } else {
        expect(() =>
          respond(state, response as Partial<ConversationResponseObservation>),
        ).toThrow(message);
      }
      expect(state).toEqual(before);
    },
  );

  it("rejects empty non-terminal simulated responses", () => {
    const state = simulated();
    expect(() => respond(state)).toThrow(
      "non-terminal simulated responses require content",
    );
  });

  it("rejects observations after terminal completion", () => {
    const complete = respond(direct(), { content: "done" });
    expect(() => observeConversation(complete, {})).toThrow(
      "cannot observe conversation in complete phase",
    );
  });

  it.each([
    [[{ role: "unknown", content: "x" }], "conversation messages"],
    [
      [{ role: "assistant", content: null }],
      "assistant messages require string content",
    ],
    [
      [
        {
          role: "assistant",
          tool_calls: [
            {
              id: "call",
              type: "function",
              function: { name: "lookup", arguments: "[]" },
            },
          ],
        },
        { role: "tool", tool_call_id: "call", content: "x" },
      ],
      "arguments must encode",
    ],
    [
      [
        {
          role: "assistant",
          tool_calls: [
            {
              id: "call",
              type: "function",
              function: { name: "lookup", arguments: "{" },
            },
          ],
        },
      ],
      "arguments must be valid",
    ],
    [
      [
        {
          role: "assistant",
          tool_calls: [
            {
              id: "call",
              type: "function",
              function: { name: "one", arguments: "{}" },
            },
            {
              id: "call",
              type: "function",
              function: { name: "two", arguments: "{}" },
            },
          ],
        },
      ],
      "IDs must be unique",
    ],
    [
      [{ role: "tool", tool_call_id: "missing", content: "x" }],
      "unknown tool call",
    ],
    [
      [
        {
          role: "assistant",
          tool_calls: [
            {
              id: "call",
              type: "function",
              function: { name: "lookup", arguments: "{}" },
            },
          ],
        },
        { role: "user", content: "too soon" },
      ],
      "must be followed",
    ],
    [
      [
        {
          role: "assistant",
          tool_calls: [
            {
              id: "call",
              type: "function",
              function: { name: "lookup", arguments: "{}" },
            },
          ],
        },
      ],
      "must be followed",
    ],
  ])("validates portable messages %#", (messages, expected) => {
    if (expected === "conversation messages") {
      expect(() => parseConversationMessages(messages)).toThrow(expected);
    } else {
      expectCoreIssue(() => parseConversationMessages(messages), expected);
    }
  });

  it("validates actions and terminal results at public boundaries", () => {
    expect(() => parseConversationAction({ source: "agent" })).toThrow(
      "conversation action",
    );
    expect(() =>
      parseConversationResult({
        phase: "complete",
        messages: [],
        output: undefined,
        output_recorded: false,
        termination: { source: "engine", reason: "direct" },
      }),
    ).toThrow("conversation result");
    expectCoreIssue(
      () =>
        parseConversationResult({
          phase: "complete",
          messages: [],
          output: "impossible",
          output_recorded: false,
          termination: { source: "engine", reason: "direct" },
        }),
      "unrecorded output",
    );
  });
});

---
name: coding-standards
description: Baseline cross-project coding conventions for naming, readability, immutability, error handling, and code-quality review. Apply these whenever writing, editing, reviewing, or refactoring code in any language — proactively, not just when the user explicitly asks for a style review. Defer to more specific frontend or backend skills for framework-specific patterns when one is available.
---

# Coding Standards & Best Practices

Baseline coding conventions applicable across all projects.

## Core Philosophy

When multiple solutions exist, prefer the one that is, in order:

1. Correct
2. Readable
3. Maintainable
4. Testable
5. Performant

Never sacrifice readability for a small performance gain unless the performance issue has been measured.

## When to Activate

- Starting a new project or module
- Reviewing code for quality and maintainability
- Refactoring existing code to follow conventions
- Enforcing naming, formatting, or structural consistency
- Setting up linting, formatting, or type-checking rules
- Onboarding new contributors to coding conventions

## Scope Boundaries

Activate this skill for:
- Descriptive naming
- Immutability defaults
- Readability, KISS, DRY, and YAGNI enforcement
- Error-handling expectations and code-smell review

Do not use this skill as the primary source for:
- React composition, hooks, or rendering patterns
- Backend architecture, API design, or database layering
- Domain-specific framework guidance

## Code Quality Principles

### 1. Readability First
Code is read far more often than it is written. Use clear variable and function names, prefer self-documenting code over comments, and keep formatting consistent.

### 2. KISS (Keep It Simple, Stupid)
Use the simplest solution that works. Avoid over-engineering and premature optimization — easy to understand beats clever.

### 3. DRY (Don't Repeat Yourself)
Extract common logic into functions and share utilities across modules. Avoid copy-paste programming.

### 4. YAGNI (You Aren't Gonna Need It)
Don't build features or generalize before they're needed. Add complexity only when required — start simple and refactor when a real need appears.

Before writing new code, resolve it in this order and stop at the first one that works:
1. **Does this need to exist at all?** A speculative requirement is handled by not building it, not by building the smallest version of it.
2. **Does this codebase already do it?** Search for an existing helper, util, or pattern before writing a new one — reimplementing something that lives a few files over is the most common source of duplication.
3. **Does the standard library cover it?** Reach for it before custom code.
4. **Does a native language or platform feature cover it?** A database constraint over app-level validation, CSS over JS, a built-in input type over a custom widget.
5. **Does an already-installed dependency cover it?** Use it before adding a new one.
6. Only then write new code — and write the least of it that solves the actual problem.

This is a reflex, not a substitute for understanding the problem: trace what the change actually needs to touch first, then resolve the ladder. Two options both work equally well → take the one higher on the list.

## Naming

```typescript
// PASS: GOOD: Descriptive names
const marketSearchQuery = 'election'
const isUserAuthenticated = true
const totalRevenue = 1000

// FAIL: BAD: Unclear names
const q = 'election'
const flag = true
const x = 1000
```

- Use nouns for types, verbs for functions.
- Prefix booleans with `is`, `has`, `can`, `should`, or `will` (e.g. `isAuthenticated`, `hasPermission`, `canRetry`).
- Collections are plural (`users`, `orders`); single items are singular (`user`, `order`).
- Use one name per concept within a file or module — don't drift between `item`, `entry`, and `record` for the same thing as you write more code.
- Prefer the shortest name that stays unambiguous in context over one that spells out the whole call path (`validateAuthRequest`, not `handleUserAuthenticationRequestValidationLogic`). Longer isn't more descriptive past the point the reader can already guess the rest.

## Function Design & Abstraction

A function should represent one logical operation, not one tiny step. Prefer keeping related logic together instead of splitting it across multiple helper functions just to shorten any one of them — every extra function, file, or wrapper layer has a reading cost, because the reader now has to jump around to follow the logic instead of reading top to bottom.

There is no preferred function or file length. **A 100-line function that keeps one coherent piece of logic together is better than ten 10-line functions that force the reader to jump between them.** Line count is not a code smell by itself — the smell is a function doing several *unrelated* things (e.g. one function that validates input, sends an email, formats a log line, and updates a cache all at once). When you spot that, split along the seams between those responsibilities, not by an arbitrary length threshold.

Only create a new function when at least one of these is true:
- The logic is reused in multiple places.
- The extracted function represents its own business concept, and naming it improves readability.
- The extraction makes testing significantly easier.

Do NOT create a function that:
- Is only called once and doesn't clarify a distinct concept.
- Simply forwards its arguments to another function and returns the result (a pass-through wrapper).
- Only exists to rename another function.
- Saves only a few lines with no gain in clarity.

```typescript
// FAIL: BAD: pass-through wrapper, adds a layer with no value
function getUser() {
  return fetchUser()
}

// PASS: GOOD: call it directly
const user = await fetchUser()
```

As general (not hard) guidelines: prefer fewer than 4 parameters and fewer than 3 levels of nesting, using guard clauses to flatten conditionals. A small amount of duplication is usually preferable to an abstraction introduced before duplication actually exists — generalize after you see the same logic appear more than once, not in anticipation of it.

Functions should either calculate something or perform a side effect — avoid mixing both where practical.

Don't add a config object, options dict, or extra parameter to make a single call site "flexible" for a future caller that doesn't exist yet — one caller gets one shape (see YAGNI above). Likewise, don't wrap a single return value in a class or `Result`-style object when the value itself is already the answer; a wrapper earns its place by holding real behavior or more than one related field, not by looking more architected.

Length is not the deciding factor for when to split, but branching complexity is: if a function has enough conditional branches that testing it thoroughly requires many setup permutations, that's a signal to extract the branch-heavy logic into its own function even if it doesn't reduce line count — testability (Core Philosophy) can require decomposition that pure "keep it coherent" reasoning wouldn't.

### Immutability Pattern

Prefer immutable updates by default. Mutation is acceptable when it's required for performance, local to a function, or the language's normal idiom — but never mutate shared state unexpectedly.

### Error Handling

Treat errors as values rather than exceptions, and handle the error first. Languages like Go, Rust, and Zig already do this natively. The TypeScript pattern below illustrates the same language-agnostic principle — don't port the exact tuple shape into a language where it isn't idiomatic; use whatever native mechanism gets you "handle the error first" (native multi-return in Go, `Result` in Rust, exceptions caught only at meaningful boundaries in Python/Java, etc.):

```typescript
type Handler<T> = [T, null] | [null, Error];

export const returnHandler = <T>(result: T): Handler<T> => {
  return [result, null] as const;
};

// This helper only converts a thrown value into an Error — it never logs.
// Logging belongs at the call site, where there's enough context (which
// resource, why it failed) to write a log line that's actually useful —
// see the Logging section below.
export const errorHandler = (error: unknown): [null, Error] => {
  if (error instanceof Error) {
    return [null, error] as const;
  }

  return [null, new Error(`Unknown error: ${String(error)}`)] as const;
};
```

Errors should never be silently ignored. When a language supports errors as values natively, use that idiom; when using exceptions, only catch where you can recover or add meaningful context. Whenever you branch on an error, return or re-throw inside that branch rather than leaving a bare comment — this is also what lets the type checker narrow the success value on the next line.

A generic `catch (e) { console.log(e) }` around code that can't meaningfully fail isn't error handling, it's hiding a failure from the type system while doing nothing about it. If you can't name the specific error you're guarding against and what you'll do differently because of it, don't add the catch.

```typescript
// PASS: GOOD: Comprehensive error handling
async function fetchData(url: string): Promise<Handler<unknown>> {
  const [response, responseError] = await fetch(url)
    .then(returnHandler)
    .catch(errorHandler);

  if (responseError) {
    console.error(`Failed to reach ${url}`, responseError);
    return [null, responseError];
  }

  // fetch() only rejects on network failure — it resolves normally for
  // HTTP error statuses like 404 or 500, so those must be checked explicitly.
  if (!response.ok) {
    const httpError = new Error(`Request to ${url} failed with status ${response.status}`);
    console.error(httpError);
    return [null, httpError];
  }

  const [json, jsonError] = await response.json()
    .then(returnHandler)
    .catch(errorHandler)

  if (jsonError) {
    console.error(`Failed to parse response from ${url}`, jsonError);
    return [null, jsonError];
  }

  return [json, null];
}

// FAIL: BAD: no error handling, and would silently mishandle HTTP error statuses anyway
async function fetchData(url) {
  const response = await fetch(url)
  return response.json()
}
```

### Async/Await

Run independent async work in parallel rather than sequentially — sequential `await` calls that don't depend on each other's results just add up latency for no benefit.

```typescript
// PASS: GOOD: Parallel execution when possible
const [users, markets, stats] = await Promise.all([
  fetchUsers(),
  fetchMarkets(),
  fetchStats()
])

// FAIL: BAD: Sequential when unnecessary
const users = await fetchUsers()
const markets = await fetchMarkets()
const stats = await fetchStats()
```

### Type Safety

Prefer reusing or extending an existing type over creating a new one, and avoid escape hatches like `any` — they remove the compiler's ability to catch mistakes for you later. The example below is TypeScript, but the principle is language-agnostic: use your language's strongest available typing (type hints in Python, generics in Java/Go, etc.) and avoid its equivalent escape hatch.

```typescript
// PASS: GOOD: Proper types
interface Market {
  id: string
  name: string
  status: 'active' | 'resolved' | 'closed'
  created_at: Date
}

function getMarket(id: string): Promise<Market> {
  // Implementation
}

// FAIL: BAD: Using 'any'
function getMarket(id: any): Promise<any> {
  // Implementation
}
```

## Comments

Assume every comment is unnecessary unless proven otherwise — write code that explains itself through good names and clear structure instead. Comments earn their place only when explaining something the code itself can't: why something exists, a business rule, a non-obvious tradeoff, a performance hack, or a workaround for a library/platform limitation. Never comment on what the code is doing, how it works, or other things a reader can see directly from the code.

Watch for these specific patterns — they're the fingerprint of code written to be generated rather than read, and they should be deleted on sight:

```typescript
// FAIL: BAD: narration — restates the line below it
// increment the retry counter
retryCount++

// FAIL: BAD: banner comment — structure should do this job, not a heading
// ---------- Helpers ----------

// FAIL: BAD: docstring that repeats the function name in English, adds nothing
/** Gets a user by id. */
function getUser(id: string) { }

// PASS: GOOD: no comment needed, the code already says this
retryCount++
function getUser(id: string) { }
```

If code needs a narration comment to be followed, that's a signal to rewrite the code — with a clearer name or structure — not to add the comment.

## Testing Standards

Not everything deserves a test. A test only pays for itself if it could plausibly catch a real bug — writing one for a trivial getter, a one-line pass-through, or a framework-generated default doesn't protect anything; it just adds a file that has to be maintained forever and makes the suite slower to read and run.

Prioritize tests for:
- Business logic and calculations (pricing, scoring, similarity, anything with real branching).
- Edge cases and error paths (empty input, missing auth, network failure).
- Code that has broken before, or that other code depends on heavily.
- Bug fixes — a regression test for the specific bug you just fixed.

Skip tests for:
- Simple getters/setters or straight pass-throughs with no logic.
- Framework boilerplate or generated code.
- Trivial one-liners where the test would just restate the implementation.

If you're unsure whether something is "worth it," ask: *could this plausibly break in a way that matters, and would this test actually catch it?* If not, don't write it.

Tests should verify behavior, not implementation, so that refactoring never forces a test rewrite.

```typescript
// Arrange / Act / Assert
test('calculates similarity correctly', () => {
  const vector1 = [1, 0, 0]
  const vector2 = [0, 1, 0]

  const similarity = calculateCosineSimilarity(vector1, vector2)

  expect(similarity).toBe(0)
})
```

Name tests by the behavior they verify, not vaguely:

```typescript
// PASS: GOOD
test('returns empty array when no markets match query', () => { })
test('throws error when OpenAI API key is missing', () => { })

// FAIL: BAD
test('works', () => { })
test('test search', () => { })
```

## Bug Fixes

A bug report names a symptom, not necessarily the cause. Before editing, find every caller of the function you're about to change — a fix applied at the shared function is a smaller diff than the same guard copy-pasted into every call site, and it's the only version that also fixes the sibling callers the report didn't mention. Patching only the path the ticket describes leaves the same bug reachable from everywhere else that calls the same code.

## Code Smell Detection

### Mixed responsibilities
A function juggling several unrelated concerns (e.g. validating input, calling an external API, and formatting output all in one place) is harder to reason about and test than one that does a single coherent thing — even if splitting it makes each piece shorter. Split along responsibility boundaries, not by line count (see Function Design above).

### Deep nesting
```typescript
// FAIL: BAD: 5+ levels of nesting
if (user) {
  if (user.isAdmin) {
    if (market) {
      if (market.isActive) {
        if (hasPermission) {
          // Do something
        }
      }
    }
  }
}

// PASS: GOOD: Early returns / guard clauses
if (!user) return
if (!user.isAdmin) return
if (!market) return
if (!market.isActive) return
if (!hasPermission) return

// Do something
```

### Magic numbers
```typescript
// FAIL: BAD: Unexplained numbers
if (retryCount > 3) { }
setTimeout(callback, 500)

// PASS: GOOD: Named constants
const MAX_RETRIES = 3
const DEBOUNCE_DELAY_MS = 500

if (retryCount > MAX_RETRIES) { }
setTimeout(callback, DEBOUNCE_DELAY_MS)
```

Never duplicate URLs, API routes, environment variable names, timeout values, or status strings — extract them into named constants instead.

Handle `null` and `undefined` explicitly with early validation. Avoid deep optional chaining that quietly hides missing data instead of surfacing it.

### Impossible-state guards
A `null`/`undefined` check on a value built two lines above, or a type check at an internal call site with exactly one caller, doesn't add safety — it adds a line the reader has to verify is actually reachable, and usually isn't. These differ from the guard clauses above: a guard clause protects against a real external possibility (missing auth, empty input); an impossible-state guard protects against something the surrounding code already guarantees. If you can't describe a concrete situation that would trigger the check, remove it — trust boundaries (Security, above) still get validated regardless.

## Logging

Logs should answer what happened, why, and on which resource — "Failed to create order 1832" beats "Error". Never log passwords, secrets, tokens, API keys, or personal information.

A log line earns its place by helping diagnose a real failure later. Skip logs that only narrate progress ("Starting processing...", "Done!") with no diagnostic value, and skip decorative content — exclamation points, emoji, "Success! 🎉" — in logs or error messages; production output is read during an incident, not a demo, and noise there buries the signal that matters.

## Security

Never trust external input — validate, sanitize, and escape it. Never expose internal errors directly to users. Never hardcode credentials, API keys, ports, or environment values; use configuration or environment variables instead. Handle edge cases so a function behaves predictably for every input, not just the happy path.

## Performance

Don't optimize before measuring. Readable O(n) code is usually better than unreadable, micro-optimized code — optimize only after profiling has identified an actual bottleneck.

## Dependencies

Avoid introducing a new dependency unless it provides significant value over the standard library. Before reaching for any dependency, new or already installed, check whether a native language or platform feature already covers it — see the resolution order under YAGNI above.

## Working With Existing Code

When modifying existing code, follow the project's current structure and conventions rather than introducing a new pattern. Don't create new folders or abstraction layers unless the task genuinely requires it, and don't refactor unrelated code while you're in there — keep the change as small as it can be while still doing the job well. Simple code beats speculatively "reusable" code; only generalize after real duplication exists, not in anticipation of it.

## Pre-Merge Checklist

Before merging, ask:
- Is the code readable, and can any names be improved?
- Can duplication be removed, or nesting be reduced?
- Is error handling complete?
- Is it tested?
- Is there dead code, or an abstraction that isn't earning its cost?
- Does a bug fix address the shared root cause, or just the one path the ticket named?
- Any comment that just narrates the line below it, any config/class added for a single caller, any impossible-state guard?

Delete unused variables, imports, functions, comments, and feature flags.

---

**Remember:** code quality is not negotiable, but rigidity is not the goal — clear, maintainable code enables rapid development and confident refactoring. When these guidelines and the immediate task pull in different directions, optimize for what makes this specific code easiest for the next person to read.

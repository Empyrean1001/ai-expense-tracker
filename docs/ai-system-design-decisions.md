# AI System Design Decisions — Expense Tracker

A study document. Each entry records an architectural decision made while designing this system, the alternatives considered, and the **generalizable principle** behind it — the building blocks of how an AI engineer designs an LLM-powered product in 2026. Read alongside the v2 spec (`docs/specs/2026-09-08-expense-tracker-v2-worldwide-design.md`).

Format per decision: **Context → Options → Decision → Why → Principle**.

---

## D-1: Use the LLM at *design time* (schema inference), not *run time* (row parsing)

> Revised by **D-15**: the "compile once, execute deterministically" idea stands, but the LLM no longer writes the whole profile — code measures the layout and the model only labels header text when code can't.

**Context.** Every bank invents its own CSV/XLSX layout. The naive AI approach is "throw each statement at the LLM and let it extract transactions."

**Options.**
1. LLM parses every row of every statement.
2. LLM inspects a sample of the file *once*, emits a declarative column-mapping profile (`BankProfile`), and deterministic code parses all rows using it — profile cached per bank fingerprint.
3. Hand-write a parser per supported bank.

**Decision.** Option 2.

**Why.** Option 1 is slow (thousands of LLM calls), non-deterministic (the same statement can parse differently twice), unauditable, and financially dangerous — a hallucinated digit in an amount is silent data corruption. Option 3 doesn't scale to "worldwide." Option 2 gets LLM flexibility at onboarding and deterministic reliability forever after: after the first statement from a bank, parsing that bank costs zero LLM calls.

**Principle.** *Compile, don't interpret.* When an LLM can produce a reusable artifact (a mapping, a config, a program) instead of doing the work item-by-item, make it write the artifact once and execute the artifact deterministically. LLM output becomes reviewable data, per-item cost drops to zero, and behavior becomes reproducible.

---

## D-2: Deterministically validate every LLM-produced artifact before trusting it

**Context.** The Schema Mapping Agent will sometimes be wrong: wrong amount column, wrong decimal separator, `MM/DD` vs `DD/MM` confusion.

**Options.**
1. Trust the profile; let bad imports surface as weird dashboards.
2. Ask the LLM to double-check its own output.
3. Mechanical validation: round-trip parse the sample rows, check date plausibility, and check **balance continuity** (`previous_balance ± amount == balance` across consecutive rows).

**Decision.** Option 3, with one LLM retry on failure (failure details fed back), then fall back to asking the human.

**Why.** LLM self-review (option 2) correlates with the original error — the model that misread the layout often "verifies" the same misreading. The balance-continuity check is the gem: it is cheap, fully deterministic, and a single invariant that simultaneously catches wrong columns, wrong sign conventions, and wrong number locales, because *any* of those errors breaks the arithmetic chain.

**Principle.** *Never trust, always verify — with checks from a different epistemic source than the model.* The strongest validators are domain invariants (accounting identities, checksums, round-trips), not more LLM. Design your data model so such invariants exist.

---

## D-3: Tiered categorization cascade — cheapest capable tier first

**Context.** Categorizing every transaction with an LLM is wasteful: most of a statement is vendors seen before.

**Decision.** A cascade: (0) user-pinned rules → (1) learned vendor cache → (2) local LLM → (3) human review. Each tier only receives what the previous tier couldn't settle.

**Why.** Latency and consistency, not just cost. A cached/ruled answer is instant and *stable across months* — the same vendor never flip-flops categories because a model was sampled twice. The LLM handles only the genuinely novel; the human handles only the genuinely ambiguous. In steady state a monthly run makes a handful of LLM calls.

**Principle.** *Escalation ladders beat monoliths.* Route work to the cheapest mechanism that can handle it — deterministic rules, then memory, then model, then human. Design the routing signal (here: cache hit + model confidence) as carefully as the tiers themselves.

---

## D-4: Human-in-the-loop is a product feature, routed by confidence — not an error path

**Context.** Some transactions cannot be resolved from data alone (is a payment to a person a loan or an auto-rickshaw fare?).

**Decision.** The model returns a calibrated confidence; anything below a configurable threshold, plus every person-to-person loan candidate, is queued for an interactive review the user completes in seconds. Corrections are written back to the vendor cache (and can be promoted to rules), so the system asks less every month. The LLM is *forbidden* from deciding lend/borrow — that is always a human call.

**Why.** Silent guessing in a finance tool destroys trust the first time it's wrong. Explicit review converts model uncertainty into a tiny, bounded user chore, and the write-back loop turns each answer into permanent training signal (without any model fine-tuning).

**Principle.** *Uncertainty is an output, route on it.* Make models emit confidence, calibrate it against evals, and design the UX so low confidence becomes a quick human decision that the system remembers. Corrections must flow back into the cheap tiers, or you'll ask the same question forever.

---

## D-5: Local-first LLM by default; capability seams for stronger models

**Context.** Bank statements are among the most sensitive documents a consumer has.

**Options.** Cloud LLM (better accuracy, data leaves the machine) · local Ollama model (private, weaker) · hybrid.

**Decision.** Local-only by default (`qwen`-class small model via Ollama), with the model behind a config seam (`config.yaml`) so any pulled model — or, in a future version, a BYO-key cloud model — swaps in without code changes. All architecture (structured output, cascade, review threshold) is designed to make a *small* model sufficient.

**Why.** Privacy is the product's core promise and a real differentiator; "your data never leaves your machine" is simple to say and verify. The accuracy gap of a 3B model is absorbed by design: rules and cache remove easy cases, tight structured prompts constrain the task, and the review queue catches the rest. Note the system-level lesson: you can ship a weaker model successfully if the *system around it* compensates.

**Principle.** *Choose the smallest model the system design lets you get away with, and keep the model swappable.* Model choice is a config value, not an architecture. Design the surrounding system (caching, validation, HITL) to be model-agnostic so capability upgrades are free.

---

## D-6: Structured output with schema validation at every model boundary

**Context.** Free-text LLM output must be parsed; parsing free text is where pipelines rot.

**Decision.** Every LLM call uses structured output (JSON mode / `with_structured_output`) against a Pydantic schema (`CategorizationResult`, `BankProfile`). Schemas give every field a safe default so a partially-conforming response degrades to "needs review" instead of crashing. Malformed output: one retry, then fall back to the review queue.

**Principle.** *Types at the boundary.* The model is an untyped process; wrap it in a typed contract. Defaults-as-degradation means schema violations become routing decisions, not exceptions.

---

## D-7: Learned memory (vendor cache) — and getting the cache key right

**Context.** Caching vendor→category mappings makes categorization consistent and cheap. But what is the key?

**Decision.** Merchants are keyed by normalized vendor name. Payments to *individuals* are keyed by `name + payment note`, because "RAHUL SHARMA" tells you nothing about what the money was for — the note ("lunch", "rent", "auto") does. Keying people by name alone would let the first note ever seen decide the category of every future payment to that person.

**Principle.** *A cache key is a claim about what determines the answer.* Before caching AI output, ask: what inputs actually determine this result? Cache on exactly those. Wrong cache keys silently freeze early model outputs into permanent wrong answers — the failure is invisible because nothing errors.

---

## D-8: Evals are the regression tests of AI behavior

**Context.** Prompts, models, and taxonomies will change. How do you know a change didn't make categorization worse in German or break Indian statement parsing?

**Decision.** A committed eval harness with golden datasets: synthetic statement files across bank layouts/locales for schema mapping, and anonymized labeled transactions for categorization. Metrics: accuracy *and calibration* (of high-confidence answers, how many are right — this is what makes the review threshold meaningful). Run before any prompt/model change ships.

**Why.** Unit tests can't assert on model behavior; evals can. Calibration matters as much as accuracy here because the entire HITL design (D-4) rests on confidence being trustworthy.

**Principle.** *No evals, no iteration.* An AI system without evals can only change by vibes. Build the golden dataset early (it's cheap when small), measure calibration not just accuracy, and treat eval regressions like failing tests.

---

## D-9: Don't use AI where a standard exists

**Context.** OFX/QFX, QIF, CAMT.053 (ISO 20022), MT940 are documented interchange formats many banks export.

**Decision.** These get small deterministic parsers. The LLM path (D-1) exists only for the anarchic formats (CSV/XLSX) where no spec constrains the layout.

**Principle.** *AI is for the absence of structure.* Reach for a spec, a library, or a parser before a model. Every place you substitute deterministic code for a model you gain speed, reproducibility, and testability for free — spend the model's flexibility only where the input is genuinely unstructured.

---

## D-10: Know what the data cannot tell you — splits are never inferred

**Context.** It's tempting to have the LLM detect shared/group expenses. But a bank statement of "you paid the restaurant ₹4,000" contains *zero signal* about whether friends owe you part of it.

**Decision.** Split tracking is a separate, on-demand, LLM-free command. The pipeline never attempts to infer it. Similarly, the P2P heuristic only *flags candidates* (person-looking payee + the pipeline couldn't explain the payment); the lend/borrow/expense decision is always the user's.

**Principle.** *Model the information-theoretic limit of your data before assigning tasks to the model.* Asking an LLM a question the input cannot answer produces confident fabrication, not insight. Where the data ends, the design must hand off to the user — explicitly.

---

## D-11: Canonical internal representation, standards-based

**Context.** Six input formats, dozens of bank dialects, many locales — downstream code cannot handle them all.

**Decision.** Everything normalizes at the ingestion boundary into one canonical `Transaction`: ISO 8601 dates, ISO 4217 currency codes, `Decimal` amounts with an explicit debit/credit direction, stable dedup hash. All downstream stages (categorization, insights, dashboard) know only this model. Also: money is `Decimal`, never `float` — binary floats cannot represent 0.10 and accumulate cent errors.

**Principle.** *Normalize at the edge; keep the core dialect-free.* The messier the input space, the more valuable a single internal schema. This is classic data engineering, and AI systems need it more, not less — prompts and evals are written against the canonical form.

---

## D-12: Orchestration layer stays thin; complexity earns its way in

**Context.** The pipeline uses LangGraph's `StateGraph`. Frameworks invite over-engineering — checkpointers, interrupts, sub-agents.

**Decision.** Every graph node is a thin wrapper over a plain, independently-testable Python function; the graph only encodes order and one conditional edge (skip everything when no new transactions). Interactive review runs as a plain blocking function *inside* a node — not LangGraph's `interrupt()`/checkpointer machinery — because `interrupt()` earns its complexity only when a run must survive process restarts, and this is a single local CLI invocation.

**Principle.** *Agent frameworks are wiring, not architecture.* Keep business logic in plain functions with injected dependencies (model, prompt fn); let the framework do sequencing only. Adopt each framework feature the day its problem actually appears, not the day the tutorial shows it.

---

## D-13: Right-size everything else around the AI (storage, integrations, UX)

Three non-AI decisions that shape the AI system:

- **Flat files, no database.** `transactions.csv` + JSON stores are grep-able, hand-editable, trivially backed up, and sufficient for one user's decades of transactions. A DB adds operational surface with no current benefit. *Storage should match the actual concurrency and volume, not the imagined ones.*
- **Manual statement upload, no open-banking APIs** (Plaid/GoCardless/Account Aggregator). APIs mean OAuth flows, credentials custody, per-region aggregator contracts, and a privacy story that contradicts local-first. Monthly manual export is a chore the target user already accepts. *Integration cost is mostly ongoing, not upfront — defer it until the product proves people want the core loop.*
- **Excel as the output surface.** The user already lives in Excel; a generated workbook is a *view* over the flat-file source of truth and can be regenerated at will. Output formats are adapters; the dashboard is not the data.

**Principle.** *AI product viability is usually decided by the non-AI 80%.* Ingestion, storage, error messages, and setup UX sink more AI products than model quality does. Budget design attention accordingly.

---

## D-14: Fail fast on infrastructure, degrade gracefully on inference

**Context.** Two very different failure classes: Ollama is down (infrastructure), vs. one response didn't parse (inference).

**Decision.** Infrastructure is checked *pre-flight* (`doctor` / reachability check before the run starts) and aborts with the exact fix command — because a dead server surfacing per-transaction would silently mark an entire run "Uncategorized": technically complete, practically garbage. Inference failures degrade per-item: retry once, then route to the review queue; mid-run progress is persisted so a crash resumes instead of restarting.

**Principle.** *Classify failures by blast radius, then pick the policy per class.* Systemic failures should stop the world loudly and early; per-item failures should degrade into the human tier. Applying either policy to the other class produces the two classic AI-pipeline disasters: the garbage-but-green run, and the crash that loses an hour of work.

---

## D-15: Measure structure with code; give the model only the language part *(revises D-1)*

**Context.** D-1 had the LLM read the first ~40 raw rows of an unknown CSV and emit a whole `BankProfile`: find the header row, copy its text, map columns, pick the date format and decimal separator. On real 595-row bank exports, the 7B local model failed every time. It invented header names ("S.No", "Timestamp" for "Sl. No.", "Transaction Date") and used them as column-map keys, so its output didn't even fit the schema. A fixed-field schema fixed the *shape* (Ollama constrains decoding to the JSON schema, so every response parsed), but not the *content*: same input, same wrong answer (header row 1 instead of 13, wrong amount convention, missing date and balance columns). The manual fallback failed too, and in a worse way. It asked the user for strptime syntax (they typed `hh:mm:ss`), a decimal separator (they guessed wrong), and "the text that ends the table". They typed the last serial number, which **silently dropped the last transaction while still passing validation**.

**Options.**
1. Better prompts / a bigger model for whole-profile inference.
2. A fixed-field schema (indices only) for the same grid-reading task.
3. Measure everything measurable with code: header row (the row before the first run of date-and-number rows), column roles (header synonyms, else cell contents), date format (try candidates against real cells), decimal separator (count `.dd` vs `,dd` endings). Settle any remaining ambiguity (which numeric column is amount vs balance; which of two `Dr / Cr` columns matters) by *generating candidate profiles and keeping the one that passes balance continuity*. Use the LLM only as a fallback, and only for what it's good at: **labelling header text** ("Out" → `debit_amount`). Code still builds and validates the profile.

**Decision.** Option 3. Option 2 was tested live and failed (the evidence is above); option 1 is the same mistake, just more expensive.

**Why.** Reading a 2-D grid of cells is spatial and counting work, which small models are weak at; deciding that "Wertstellung" means value date is language work, which they're good at. Moving each sub-task to the cheapest mechanism that does it reliably made a normal bank statement need **zero** LLM calls (60 ms, fully deterministic). The balance-continuity invariant from D-2 got promoted from a *gate* to a *search oracle*: instead of asking "is this one guess right?", code generates every plausible combination and the invariant picks the right one. The interactive fallback got the same treatment. It's pre-filled from detection, every answer is checked against the file as it's given, date patterns are accepted in plain `dd-mm-yyyy` form, and questions code can answer (decimal separator, table end) are no longer asked.

**Principle.** *Use the model for the part of the problem that is actually language.* Before giving an LLM a task, split it into what can be measured from the data, what can be verified by an invariant, and what genuinely needs semantics; then give the model only the last part. A corollary for human-in-the-loop design: **every question you ask a person is an input surface that needs validation**, and the best question is one you didn't need to ask. A plausible-but-wrong human answer that passes validation is worse than an LLM error, because no one suspects it.

---

## D-16: Give the model the facts you already have; check its answer against them

**Context.** A real ₹106 auto-rickshaw fare came back as *Income / Interest* at confidence 1.0, so the review step would never have shown it to the user. Two causes. First, the prompt never said whether money went out or came in, although the statement states it on every row. Second, after the direction was added, the model *still* said Income: testing one variable at a time isolated the single word `auto`. With the note `cab`, the same row was categorized correctly. The model read "auto" as *automatic* (auto-debit, auto-sweep interest), not as an auto-rickshaw.

**Decision.**
1. The prompt states direction and amount ("Money: OUT of the account, you paid 106.00 INR").
2. A deterministic invariant, Income ⇔ money in, is checked after *every* automatic tier (rules, cache, model). A contradicting answer becomes Uncategorized with confidence 0, goes to review, and is never cached.
3. Cache keys include direction, so a refund from SWIGGY doesn't reuse the purchase's "Food & Dining" entry (which the invariant would then reject on every refund).
4. Regional vocabulary the model gets wrong goes into the locale hint pack, explicitly ("'auto' means an auto-rickshaw ride, never an automatic payment"), with an eval case that pins it.

**Why.** The model's confidence was 1.0 on an answer the data proves impossible. Confidence-routed review (D-4) can't catch an error the model is sure of; only a check from outside the model can (D-2 again, now at the categorization boundary). On the real 6-month statement, the invariant overrode 10 answers and left 0 contradictions.

**Two side lessons.**
- **Fixing one bug can unmask another.** Parsing UPI notes correctly made 77 payments look like "a person paid with no note", i.e. possible loans, and half were businesses ("ZOMATO LIMITED", "APOLLO PHARMAC"). The old note bug had been hiding a weak person-vs-business check, because a reference number counted as a note. Measuring the fix's impact on real data before shipping it is what caught this.
- **The note must be what the payer typed.** App boilerplate ("UPI", "Payment for", "Pay via Razorpay") isn't a note, and treating it as one misleads both the model and the loan detector.

**Principle.** *Never make the model infer what you already know, and never trust its confidence over a fact.* Put every known fact in the prompt, and enforce the facts that imply hard constraints in code after the model answers. When a small model fails on one input, isolate the token before rewriting the prompt: the fix is often one line of domain knowledge.

---

## Reading list mapping (the 2026 AI-engineer's checklist)

If you retain nothing else, the building blocks in dependency order:

1. **Canonical data model first** (D-11) — everything else is written against it.
2. **Deterministic before probabilistic** (D-9) — carve out what needs no model.
3. **LLM as artifact-compiler where possible** (D-1) + **invariant validation** (D-2) — but measure structure with code and give the model only the language part; invariants double as a search oracle (D-15).
4. **Cascade with routing signals** (D-3) + **correct cache keys** (D-7) + **known facts in the prompt, hard constraints checked in code** (D-16).
5. **Typed model boundaries** (D-6).
6. **Confidence-routed HITL with write-back learning** (D-4) and honesty about data limits (D-10).
7. **Model swappability & smallest-sufficient-model** (D-5).
8. **Evals before iteration** (D-8).
9. **Thin orchestration** (D-12), **right-sized infra** (D-13), **failure taxonomy** (D-14).

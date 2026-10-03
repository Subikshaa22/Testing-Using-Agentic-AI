# AI-assisted MBPP unit-testing pipeline

This implements the metrics side of the agentic pipeline required by the project brief. GitHub Copilot Chat or another LLM creates and executes the tests; this Python pipeline only analyzes paths, validates the returned evidence, calculates metrics, and writes artifacts.

1. **LLM/Copilot agent** produces and executes the implementation tests.
2. **Metrics pipeline** identifies static paths and evaluates the LLM-produced evidence.

The official MBPP JSONL file is stored at `data/mbpp.jsonl`; the reduced input used by the pipeline is `data/mbpp_modified.jsonl`.

## Run

```powershell
python pipeline.py --limit 10 --copilot-prompt-output results/copilot_prompt.md
```

The pipeline processes the first 10 MBPP programs by default. Copilot must return an LLM response JSON file before metrics can be calculated.

The detailed test-generation instructions are maintained in [`prompts/test_generator_prompt.txt`](prompts/test_generator_prompt.txt). The prompt requires the test-generation agent to identify every feasible branch and create assert cases that exercise both outcomes and relevant boundary inputs.

The objective can be selected per run:

```powershell
# Coverage criterion
python pipeline.py --objective coverage --coverage-criterion "all branches" --llm-response data/copilot_generated_tests.json

# Function property
python pipeline.py --objective property --property "sorted output preserves all input elements" --llm-response data/copilot_generated_tests.json

# Statistical estimation over repeated samples
python pipeline.py --objective statistical --runs 10 --llm-response data/copilot_generated_tests.json
```

The response passed to `--llm-response` must include the tests, execution results, and covered path IDs supplied by Copilot or another LLM. The pipeline does not execute tests itself.

## GitHub Copilot Chat workflow

The pipeline cannot call the VS Code Copilot Chat UI directly. It creates a batch prompt for Copilot Chat, and then evaluates the JSON test suites and execution evidence that Copilot returns. This keeps both test creation and test execution in Copilot.

First generate a prompt for the first 10 records from `mbpp_modified.jsonl`:

```powershell
python pipeline.py --dataset data/mbpp_modified.jsonl --limit 10 --objective coverage --coverage-criterion "all branches" --chat-prompt "Cover every feasible branch, including empty and boundary inputs." --copilot-prompt-output results/copilot_coverage_prompt.md
```

Open `results/copilot_coverage_prompt.md` in Copilot Chat. Ask Copilot to follow the embedded output format and save its JSON response as `data/copilot_generated_tests.json`. Then execute and score it:

```powershell
python pipeline.py --dataset data/mbpp_modified.jsonl --limit 10 --objective coverage --llm-response data/copilot_generated_tests.json --output results/copilot_coverage_results.json
```

Example prompts for the three project specifications:

1. **Coverage criterion**

	`Use the coverage objective. Identify every feasible statement, loop, and decision branch in each implementation. Generate assert tests that cover all branches, including true and false decisions, zero and non-zero loop iterations, early returns, empty inputs, and boundary values.`

2. **Function property**

	`Use the property objective. For each function, state the expected behavior as an observable property, then generate assert tests for normal, edge, and counterexample inputs. For a list-search function, test found elements, missing elements, duplicates, empty lists, and boundary positions.`

3. **Statistical estimation**

	`Use the statistical objective. Generate a complete candidate test suite for every task and make each assertion independently checkable. Treat a suite as correct only when all assertions pass and the selected coverage criterion is satisfied. Return the required JSON object so the pipeline can estimate the probability that at least one suite is correct across repeated samples.`

The response file must be a JSON object like `{"1": ["assert ..."], "2": ["assert ..."]}` with one task ID entry for every task in the batch.

For a single natural-language request, use `--chat-prompt`. The pipeline extracts the program number and infers the objective from phrases such as `property objective`:

```powershell
python pipeline.py `
	--dataset data/mbpp_modified.jsonl `
	--chat-prompt "For program 1 in mbpp.jsonl, use the property objective. For each function, state the expected behavior as an observable property, then generate assert tests for normal, edge, and counterexample inputs. For a list-search function, test found elements, missing elements, duplicates, empty lists, and boundary positions." `
	--copilot-prompt-output results/programs/program_001/copilot_prompt.md
```

Open the generated prompt in Copilot Chat, execute the tests with Copilot's available tools, and save Copilot's JSON response as `data/copilot_generated_tests.json`. Then run:

```powershell
python pipeline.py `
	--dataset data/mbpp_modified.jsonl `
	--chat-prompt "For program 1 in mbpp.jsonl, use the property objective." `
	--llm-response data/copilot_generated_tests.json `
	--output results/copilot_program_001_results.json
```

The pipeline places the implementation, generated tests, individual test results, and `statistics.json` under `results/programs/program_001/`. The Copilot response must contain the selected task ID, for example `{"1": ["assert ..."]}`.

Each program also gets its own folder under `results/programs/program_###/` containing:

- `implementation.py`: the program under test.
- `generated_tests.py` and `generated_tests.json`: the test cases used for that program.
- `test_results.json`: individual pass/fail and duration results.
- `statistics.json`: per-program verdict, pass rates, mutation score, total path estimate, covered paths, path coverage, and `GOOD` or `NEEDS_REVIEW` quality verdict.

Quality is assessed from execution results and covered path IDs supplied by the LLM. Path coverage counts static branch and loop nodes; `if`, `while`, and `for` behavior are used to estimate the total possible paths. A task is marked `GOOD` only when the LLM reports a correct suite and all estimated paths are covered. Missing execution evidence is reported as `NEEDS_REVIEW`.

### Quality Definitions

- **Mutant:** A deliberately modified version of the implementation, such as changing `a + b` to `a - b`. A mutant is killed when a test fails against it. The mutation score is `mutants_killed / mutants_generated`.
- **Statistical runs:** The number of candidate test-suite samples used for statistical estimation. For example, `--runs 10` requests an estimate across 10 AI-generated samples.
- **Suite correctness:** A suite is correct when its tests pass, all measured paths are covered, and mutation quality is acceptable when mutants are available. A passing test suite with incomplete path coverage is marked `NEEDS_REVIEW`.
- **Quality verdict:** `GOOD` means the suite satisfies the configured quality checks. `NEEDS_REVIEW` means at least one check, such as path coverage or mutation score, was not satisfied.
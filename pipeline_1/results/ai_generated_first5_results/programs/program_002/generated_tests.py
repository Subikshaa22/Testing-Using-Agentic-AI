# Copilot Chat prompt:
# For the first five MBPP programs, use the coverage objective and cover all branches, including normal, edge, and boundary inputs.
# Tests were generated and executed by the AI agent.

assert similar_elements((1, 2), (3, 4)) == ()
assert set(similar_elements((1, 2, 3), (2, 3, 4))) == {2, 3}
assert set(similar_elements((5, 5, 6), (5, 6))) == {5, 6}

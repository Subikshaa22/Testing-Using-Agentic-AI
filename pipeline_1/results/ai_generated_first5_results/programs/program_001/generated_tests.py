# Copilot Chat prompt:
# For the first five MBPP programs, use the coverage objective and cover all branches, including normal, edge, and boundary inputs.
# Tests were generated and executed by the AI agent.

assert min_cost([[7]], 0, 0) == 7
assert min_cost([[1, 2, 3], [4, 8, 2], [1, 5, 3]], 0, 2) == 6
assert min_cost([[1, 2, 3], [4, 8, 2], [1, 5, 3]], 2, 0) == 6
assert min_cost([[1, 2, 3], [4, 8, 2], [1, 5, 3]], 2, 2) == 8

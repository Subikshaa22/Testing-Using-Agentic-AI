"""Statistical estimators used by the statistical-testing mode."""
import math


def pass_at_k(n, c, k):
    """Unbiased estimate of P(at least one of k sampled suites is correct).

    n = suites generated, c = suites that were correct, k = suites drawn.
    pass@k = 1 - C(n-c, k) / C(n, k)   (Chen et al., 2021, "Evaluating Large
    Language Models Trained on Code"). Unlike 1-(1-c/n)^k, it is unbiased.
    """
    if k > n:
        raise ValueError(f"k={k} cannot exceed n={n}")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def wilson_interval(successes, trials, z=1.96):
    """95% Wilson score confidence interval for a binomial proportion."""
    if trials == 0:
        return 0.0, 0.0
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)

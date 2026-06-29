# Contributing to Coding Agent

Thank you for your interest in contributing! This document provides guidelines and instructions for contributing.

## 🚀 Getting Started

### Prerequisites

- Python 3.11+
- Git

### Setup

```bash
# Fork and clone
git clone https://github.com/YOUR_USERNAME/coding-agent.git
cd coding-agent

# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest
```

## 📝 Development Workflow

1. **Create a branch** from `main`
   ```bash
   git checkout -b feature/your-feature
   ```

2. **Make your changes** — Follow the coding standards below

3. **Write tests** — All new features should have tests

4. **Run the benchmark** — Ensure your changes don't break existing functionality
   ```bash
   python benchmarks/benchmark.py
   ```

5. **Commit** with a clear message
   ```bash
   git commit -m "feat: add new tool for X"
   ```

6. **Push and create PR**
   ```bash
   git push origin feature/your-feature
   ```

## 🎯 Coding Standards

### Python Style

- Follow [PEP 8](https://peps.python.org/pep-0008/)
- Use type hints for all function signatures
- Use `pathlib.Path` instead of `os.path`
- Use `async/await` for I/O operations
- Prefer `dataclass` for data structures

### Docstrings

Use Google-style docstrings:

```python
def function_name(param1: str, param2: int) -> bool:
    """Short description.
    
    Longer description if needed.
    
    Args:
        param1: Description of param1.
        param2: Description of param2.
        
    Returns:
        Description of return value.
        
    Raises:
        ValueError: When something is wrong.
    """
```

### Type Hints

```python
# Good
def process(items: list[str]) -> dict[str, int]:
    ...

# Bad
def process(items):
    ...
```

## 🧪 Testing

### Running Tests

```bash
# All tests
pytest

# Specific test file
pytest tests/test_agent.py

# With coverage
pytest --cov=coding_agent
```

### Writing Tests

```python
import pytest
from coding_agent.core.agent import AgentLoop

@pytest.mark.asyncio
async def test_agent_loop():
    """Test agent loop basic functionality."""
    agent = AgentLoop(api_key="test-key")
    # ... test logic
    assert result is not None
```

## 📊 Benchmark

### Adding a New Test Case

1. Open `benchmarks/benchmark.py`
2. Add a new `BenchmarkCase` to `BENCHMARK_CASES`:

```python
BenchmarkCase(
    id="your_test_id",
    category="your_category",
    difficulty="easy|medium|hard",
    task="Description of what the agent should do",
    setup_files={
        "filename.py": "file content...",
    },
    verify_fn=lambda d: your_verify_function(d),
    max_turns=10,
    timeout=120,
),
```

### Verify Functions

Verify functions should return `(passed: bool, detail: str)`:

```python
def your_verify_function(workdir: str) -> tuple[bool, str]:
    """Check if the agent completed the task correctly."""
    p = Path(workdir, "expected_file.py")
    if not p.exists():
        return False, "File not created"
    
    content = p.read_text()
    if "expected_content" in content:
        return True, "Success"
    return False, "Content mismatch"
```

## 🐛 Bug Reports

When reporting bugs, please include:

1. **Description** — What happened vs. what you expected
2. **Steps to reproduce** — Minimal steps to trigger the bug
3. **Environment** — OS, Python version, package versions
4. **Logs** — Any error messages or stack traces

## 💡 Feature Requests

When suggesting features:

1. **Use case** — Why is this feature needed?
2. **Proposed solution** — How should it work?
3. **Alternatives** — What other approaches did you consider?

## 📚 Resources

- [Python Documentation](https://docs.python.org/3/)
- [asyncio Documentation](https://docs.python.org/3/library/asyncio.html)
- [pytest Documentation](https://docs.pytest.org/)

## 📞 Questions?

Feel free to open an issue for any questions!

## 🙏 Thank You

Your contributions make this project better for everyone!

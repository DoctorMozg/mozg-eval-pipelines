______________________________________________________________________

paths:

- '\*\*/\*.py'

______________________________________________________________________

<!-- mz-rule:python-conventions v=0.53.0 -->

## Testing

Put shared setup in pytest fixtures (in `conftest.py` when several modules use it) rather than inside individual test functions.

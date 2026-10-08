"""SQL Security Validator using SQLGlot.

This module provides SQL validation and security checking using SQLGlot parser.
It ensures that only safe, read-only queries are executed and blocks potentially
dangerous operations.
"""

import re
from typing import ClassVar

import sqlglot
from sqlglot import exp

from pg_mcp.config.settings import SecurityConfig
from pg_mcp.models.errors import SecurityViolationError, SQLParseError
from pg_mcp.models.schema import DatabaseSchema


class SQLValidator:
    """SQL security validator using SQLGlot for parsing and validation.

    This validator ensures queries are safe by:
    - Allowing only SELECT statements
    - Blocking dangerous functions (pg_sleep, file operations, etc.)
    - Preventing access to blocked tables and columns
    - Rejecting multi-statement queries
    - Validating subquery safety
    """

    # Allowed statement types at the top level (including set operations)
    ALLOWED_STATEMENT_TYPES: ClassVar = {exp.Select, exp.Union, exp.Intersect, exp.Except}

    # Forbidden statement types
    FORBIDDEN_STATEMENT_TYPES: ClassVar = {
        exp.Insert,
        exp.Update,
        exp.Delete,
        exp.Drop,
        exp.Create,
        exp.Alter,
        exp.Grant,
        exp.Revoke,
        exp.Set,
        exp.Command,
        exp.Use,
        exp.Merge,
    }

    # Built-in dangerous PostgreSQL functions
    BUILTIN_DANGEROUS_FUNCTIONS: ClassVar = {
        "pg_sleep",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_open",
        "pg_write_file",
        "pg_execute_sql",
        "copy_from",
        "copy_to",
        "set_config",
        "query_to_xml",
        "query_to_xmlschema",
        "query_to_xml_and_xmlschema",
        "table_to_xml",
        "schema_to_xml",
        "database_to_xml",
        "lo_get",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
    }

    def __init__(
        self,
        config: SecurityConfig,
        blocked_tables: list[str] | None = None,
        blocked_columns: list[str] | None = None,
        allow_explain: bool | None = None,
    ) -> None:
        """Initialize SQL validator.

        Args:
            config: Security configuration containing blocked functions and settings.
            blocked_tables: Optional list of table names to block access to.
            blocked_columns: Optional list of column names to block access to.
            allow_explain: Whether to allow EXPLAIN statements.
        """
        self.config = config
        self.blocked_tables = {
            t.lower() for t in (config.blocked_tables if blocked_tables is None else blocked_tables)
        }
        self.blocked_columns = {
            c.lower()
            for c in (config.blocked_columns if blocked_columns is None else blocked_columns)
        }
        self.allow_explain = config.allow_explain if allow_explain is None else allow_explain

        # Combine built-in dangerous functions with custom blocked functions
        self.blocked_functions = self.BUILTIN_DANGEROUS_FUNCTIONS | {
            f.lower() for f in config.blocked_functions
        }

    def validate(self, sql: str) -> tuple[bool, str | None]:
        """Validate SQL query for security compliance.

        Args:
            sql: SQL query string to validate.

        Returns:
            Tuple of (is_valid, error_message). If valid, error_message is None.
        """
        try:
            self.validate_or_raise(sql)
            return (True, None)
        except (SecurityViolationError, SQLParseError) as e:
            return (False, str(e))

    def validate_or_raise(self, sql: str) -> None:
        """Validate SQL query and raise exception on violation.

        Args:
            sql: SQL query string to validate.

        Raises:
            SQLParseError: If SQL cannot be parsed.
            SecurityViolationError: If SQL violates security constraints.
        """
        # Check for empty or whitespace-only SQL
        if not sql or not sql.strip():
            raise SQLParseError("SQL query cannot be empty")

        # Parse SQL using SQLGlot
        try:
            parsed = sqlglot.parse(sql, read="postgres")
        except Exception as e:
            raise SQLParseError(f"Failed to parse SQL: {e}") from e

        # Check for multiple statements
        if len(parsed) > 1:
            raise SecurityViolationError(
                "Multiple statements not allowed. Only single SELECT queries are permitted."
            )

        if not parsed:
            raise SQLParseError("No valid SQL statement found")

        statement = parsed[0]

        # Check for null or empty statement (e.g., comment-only SQL)
        if statement is None:
            raise SQLParseError("No valid SQL statement found")

        # Handle EXPLAIN statements (parsed as Command in sqlglot 28.5.0)
        if isinstance(statement, exp.Command):
            # Check if it's an EXPLAIN command
            cmd_name = str(statement.this).upper() if statement.this else ""
            if cmd_name == "EXPLAIN":
                if not self.allow_explain:
                    raise SecurityViolationError("EXPLAIN statements are not allowed")
                # Support only plain EXPLAIN SELECT/WITH. Options such as ANALYZE
                # can execute the query and must never bypass the normal checks.
                expression = statement.args.get("expression")
                inner = expression.this if isinstance(expression, exp.Literal) else ""
                if not isinstance(inner, str) or not re.match(r"\s*(SELECT|WITH)\b", inner, re.I):
                    raise SecurityViolationError("Only plain EXPLAIN SELECT/WITH is allowed")
                self.validate_or_raise(inner)
                return
            else:
                # Other commands are not allowed
                raise SecurityViolationError(
                    f"Command '{cmd_name}' is not allowed. Only SELECT queries are permitted."
                )

        # Perform security checks
        if error := self._check_statement_type(statement):
            raise SecurityViolationError(error)

        # Inspect the entire tree, including writable CTE bodies and SELECT INTO.
        for node in statement.walk():
            if isinstance(node, (*self.FORBIDDEN_STATEMENT_TYPES, exp.Into, exp.Lock)):
                raise SecurityViolationError(
                    f"{type(node).__name__.upper()} is not allowed in read-only queries"
                )

        if error := self._check_dangerous_functions(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_tables(statement):
            raise SecurityViolationError(error)

        if error := self._check_blocked_columns(statement):
            raise SecurityViolationError(error)

        if error := self._check_subquery_safety(statement):
            raise SecurityViolationError(error)

    def _check_statement_type(self, statement: exp.Expression) -> str | None:
        """Check if statement type is allowed.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Check for forbidden statement types
        for forbidden_type in self.FORBIDDEN_STATEMENT_TYPES:
            if isinstance(statement, forbidden_type):
                stmt_name = forbidden_type.__name__.upper()
                return f"{stmt_name} statements are not allowed. Only SELECT queries are permitted."

        # Ensure statement is an allowed type (SELECT or set operations)
        if not isinstance(statement, tuple(self.ALLOWED_STATEMENT_TYPES)):
            stmt_type = type(statement).__name__
            return f"Statement type {stmt_type} is not allowed. Only SELECT queries are permitted."

        return None

    def _check_dangerous_functions(self, statement: exp.Expression) -> str | None:
        """Check for use of blocked/dangerous functions.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        # Find all function calls in the query
        for func in statement.find_all(exp.Func):
            func_name = (func.name if isinstance(func, exp.Anonymous) else func.key).lower()

            if func_name in self.blocked_functions:
                return f"Function '{func_name}' is blocked for security reasons"

        return None

    def _check_blocked_tables(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked tables.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_tables:
            return None

        # Find all table references
        for table in statement.find_all(exp.Table):
            table_name = table.name.lower() if table.name else ""

            qualified = f"{table.db.lower()}.{table_name}" if table.db else table_name
            if (
                table_name in self.blocked_tables
                or qualified in self.blocked_tables
                or any(t.rsplit(".", 1)[-1] == table_name for t in self.blocked_tables)
            ):
                return f"Access to table '{table_name}' is not allowed"

        return None

    def _check_blocked_columns(self, statement: exp.Expression) -> str | None:
        """Check for access to blocked columns.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        if not self.blocked_columns:
            return None

        # Fail closed for wildcard/whole-row access. Qualified policy names also
        # block the bare column name so aliases, CTEs and subqueries cannot hide it.
        blocked_names = {name.rsplit(".", 1)[-1] for name in self.blocked_columns}
        table_aliases = {t.alias_or_name.lower() for t in statement.find_all(exp.Table)}
        table_aliases |= {q.alias.lower() for q in statement.find_all(exp.Subquery) if q.alias}
        for star in statement.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                return "Wildcard access is not allowed when columns are restricted"

        # Find all column references
        for column in statement.find_all(exp.Column):
            column_name = column.name.lower() if column.name else ""

            # Check for exact match
            if column_name in blocked_names:
                return f"Access to column '{column_name}' is not allowed"
            if not column.table and column_name in table_aliases:
                return "Whole-row access is not allowed when columns are restricted"

        return None

    def _check_subquery_safety(self, statement: exp.Expression) -> str | None:
        """Check that all subqueries only contain SELECT statements.

        Args:
            statement: Parsed SQL statement.

        Returns:
            Error message if check fails, None otherwise.
        """
        for subquery in statement.find_all(exp.Subquery):
            if not isinstance(subquery.this, tuple(self.ALLOWED_STATEMENT_TYPES)):
                return "Subqueries must contain only SELECT statements"

        return None

    def filter_schema(self, schema: DatabaseSchema) -> DatabaseSchema:
        """Hide blocked tables and columns from the LLM without mutating the cache."""
        filtered = schema.model_copy(deep=True)
        column_names = {c.rsplit(".", 1)[-1] for c in self.blocked_columns}
        table_names = {t.rsplit(".", 1)[-1] for t in self.blocked_tables}
        filtered.tables = [t for t in filtered.tables if t.table_name.lower() not in table_names]
        for table in filtered.tables:
            table.columns = [c for c in table.columns if c.name.lower() not in column_names]
            table.foreign_keys = [
                fk
                for fk in table.foreign_keys
                if fk.column_name.lower() not in column_names
                and fk.referenced_table.lower().rsplit(".", 1)[-1] not in table_names
                and fk.referenced_column.lower() not in column_names
            ]
            table.indexes = [
                idx
                for idx in table.indexes
                if not any(c.lower() in column_names for c in idx.columns)
            ]
        return filtered

    def normalize_sql(self, sql: str) -> str:
        """Normalize SQL query to a canonical form.

        This removes extra whitespace, standardizes formatting, and makes
        queries easier to compare or cache.

        Args:
            sql: SQL query string to normalize.

        Returns:
            Normalized SQL string.

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            # Generate normalized SQL
            return parsed.sql(dialect="postgres", pretty=False)
        except Exception as e:
            raise SQLParseError(f"Failed to normalize SQL: {e}") from e

    def extract_tables(self, sql: str) -> list[str]:
        """Extract all table names referenced in the SQL query.

        Args:
            sql: SQL query string.

        Returns:
            List of table names (in lowercase).

        Raises:
            SQLParseError: If SQL cannot be parsed.
        """
        try:
            parsed = sqlglot.parse_one(sql, read="postgres")
            tables = []

            for table in parsed.find_all(exp.Table):
                if table.name:
                    tables.append(table.name.lower())

            return sorted(set(tables))
        except Exception as e:
            raise SQLParseError(f"Failed to extract tables: {e}") from e

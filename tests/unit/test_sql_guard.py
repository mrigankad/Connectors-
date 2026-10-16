from __future__ import annotations

import pytest

from wayam_connectors.connectors.postgresql.bounded_query.sql_guard import guard_read_only, strip_sql_comments_and_strings, wrap_with_limit
from wayam_connectors.contracts.errors import PolicyError, ValidationError


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select a, b from crm.accounts where id = %(id)s;",
        "WITH x AS (SELECT 1 AS n) SELECT n FROM x",
        "SELECT 'delete from t' AS literal",  # keyword inside a string literal is fine
        'SELECT "update" FROM crm."drop table"',  # keyword as quoted identifier is fine
        "SELECT 1 -- insert into t\n",
        "SELECT /* update x */ 2",
        "SELECT $$insert$$",
        "VALUES (1, 2)",
        "TABLE crm.accounts",
    ],
)
def test_allowed_reads(sql):
    assert guard_read_only(sql)


@pytest.mark.parametrize(
    ("sql", "err"),
    [
        ("INSERT INTO t VALUES (1)", PolicyError),
        ("SELECT 1; DROP TABLE t", PolicyError),
        ("SELECT 1; SELECT 2", PolicyError),
        ("SELECT * FROM t FOR UPDATE", PolicyError),
        ("SELECT pg_sleep(10)", PolicyError),
        ("SELECT 1 INTO newtable", PolicyError),
        ("SELECT nextval('s')", PolicyError),
        ("UPDATE t SET a=1 RETURNING *", PolicyError),
        ("EXPLAIN ANALYZE SELECT 1", PolicyError),
        ("COPY t TO '/tmp/x'", PolicyError),
        ("SET statement_timeout = 0", PolicyError),
        ("select pg_read_file('/etc/passwd')", PolicyError),
        ("SELECT * FROM dblink('x', 'y')", PolicyError),
        ("DO $$ BEGIN END $$", PolicyError),
        ("", ValidationError),
        ("   ", ValidationError),
        ("SELECT 'unterminated", ValidationError),
        ("SELECT $$oops", ValidationError),
        ("SELECT 1\x00", ValidationError),
        ("SELECT " + "1," * 20_000 + "1", PolicyError),
    ],
)
def test_rejected(sql, err):
    with pytest.raises(err):
        guard_read_only(sql)


def test_strip_and_wrap():
    assert strip_sql_comments_and_strings("select 'a''b', \"q\" -- c\n/* d /* e */ f */ 1") == "select '', \"x\" \n  1"
    assert wrap_with_limit("SELECT 1", 10) == "SELECT * FROM (SELECT 1) AS wayam_bounded LIMIT 11"
    with pytest.raises(ValidationError):
        wrap_with_limit("SELECT 1", 0)

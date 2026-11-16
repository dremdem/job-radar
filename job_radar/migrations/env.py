"""Alembic environment: how migrations connect to the database.

The URL comes from the Alembic config (``db.upgrade_database`` sets it) or, when run by
hand, from the app settings. Migrations compare against ``models.Base.metadata`` and use
batch mode, because SQLite cannot alter most columns in place. Run them with
``job-radar db upgrade``. See docs/data-model.md.
"""

import alembic.context as context
import sqlalchemy as sa
import sqlalchemy.pool as sa_pool

import job_radar.config as jr_config
import job_radar.models as models

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", jr_config.get_settings().database_url)

target_metadata = models.Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = sa.engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=sa_pool.NullPool,
    )
    with connectable.connect() as connection:
        # Batch mode: SQLite can't ALTER most things in place.
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

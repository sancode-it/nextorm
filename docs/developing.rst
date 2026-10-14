Developing NextORM
==================

Community
---------

To keep in touch with the NextORM community, you can join the `NextORM Discord channel`_.
Here you can ask questions, share your experiences, and get help from other developers.
You get also the latest news and updates about NextORM.

To contribute to NextORM, you can visit the `NextORM GitHub repository`_.

.. _`NextORM Discord channel`: https://discord.gg/PevUtddqCM
.. _`NextORM GitHub repository`: https://github.com/sancode-it/nextorm


Install NextORM for development
--------------------------------

.. highlight:: console

Clone NextORM from github::

  $ git clone git@github.com:sancode-it/nextorm.git

If this doesn't work and you get an error 'Permission denied (publickey)',
you need to upload your ssh public key to github_.

Then go to the nextorm directory::

  $ cd nextorm

Install NextOrm with PDM (Python Development Master) for development.
Make sure you have PDM installed::

  $ pdm install --dev

.. _github: https://docs.github.com/en/authentication/connecting-to-github-with-ssh


Code quality checks
-------------------

Morepath uses Ruff_ for linting, import sorting, and code formatting.

Run the linting checks from the project directory with::

  $ pdm lint

To fix linting issues with ruff::

  $ pdm fix

To format the code with Ruff, run::

  $ pdm format

Ruff uses the project settings in ``pyproject.toml``.

To run the checks automatically on staged Python files before each
commit, enable the repository's optional Git hook once in your clone::

  $ git config core.hooksPath .githooks

The hook checks staged Python contents without modifying files.

It runs ``pdm run ruff format`` on each staged file, then runs ``pdm run ruff check``
on the formatted contents. On failure, it lists the affected files and suggests
``pdm run ruff format`` and/or ``pdm run ruff check --fix`` on all of them.

Review and stage the changes before committing again.

.. _Ruff: https://docs.astral.sh/ruff/


Running the tests
-----------------

You can run the tests with `pytest`_::

  $ pdm test

To check the test coverage, run::

  $ pdm coverage

To generate test coverage information as HTML do::

  $ pdm coverage_html

You can then point your web browser to the ``htmlcov/index.html`` file
in the project directory and click on modules to see detailed coverage
information.

.. _`pytest`: https://pytest.org


Type checking
-------------

Morepath uses mypy_ and pyright_ for type checking. Run either checker
from the project directory::

  $ pdm mypy
  $ pdm pyright

To run both checkers at once, you can use the ``typecheck`` script::

  $ pdm typecheck

Both checkers use the settings in ``pyproject.toml``.

.. _mypy: https://mypy.readthedocs.io/
.. _pyright: https://microsoft.github.io/pyright/


Running the documentation tests
-------------------------------

The documentation contains code. Check these code snippets using this command::

  $ pdm docs-test


Building the HTML documentation
-------------------------------

To build the HTML documentation (output in ``docs/_build/html``), run::

  $ pdm docs-html

For removing the built documentation, run::

  $ pdm docs-clean

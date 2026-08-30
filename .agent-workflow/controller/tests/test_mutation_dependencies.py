from __future__ import annotations

import unittest

from td_controller.mutation_dependencies import (
    MutationDependencyError,
    build_mutation_dependencies,
)


class FalseyBroker:
    def __bool__(self):
        return False

    def run(self, request):
        return request


class FalseyApplier:
    def __bool__(self):
        return False

    def apply(self, fixture, handle, proposal):
        return None


class Factory:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class MutationDependenciesTests(unittest.TestCase):
    def test_falsey_supplied_dependencies_are_preserved_exactly(self) -> None:
        broker, applier = FalseyBroker(), FalseyApplier()
        broker_factory = Factory(object())
        applier_factory = Factory(object())

        selected = build_mutation_dependencies(
            broker=broker, applier=applier,
            broker_factory=broker_factory, applier_factory=applier_factory,
        )

        self.assertIs(selected.broker, broker)
        self.assertIs(selected.applier, applier)
        self.assertEqual(broker_factory.calls, 0)
        self.assertEqual(applier_factory.calls, 0)

    def test_none_constructs_each_default_exactly_once(self) -> None:
        broker, applier = FalseyBroker(), FalseyApplier()
        broker_factory, applier_factory = Factory(broker), Factory(applier)

        selected = build_mutation_dependencies(
            broker_factory=broker_factory, applier_factory=applier_factory
        )

        self.assertIs(selected.broker, broker)
        self.assertIs(selected.applier, applier)
        self.assertEqual(broker_factory.calls, 1)
        self.assertEqual(applier_factory.calls, 1)

    def test_partial_injection_constructs_only_missing_dependency(self) -> None:
        broker, applier = FalseyBroker(), FalseyApplier()
        broker_factory, applier_factory = Factory(object()), Factory(applier)

        selected = build_mutation_dependencies(
            broker=broker, broker_factory=broker_factory,
            applier_factory=applier_factory,
        )

        self.assertIs(selected.broker, broker)
        self.assertIs(selected.applier, applier)
        self.assertEqual(broker_factory.calls, 0)
        self.assertEqual(applier_factory.calls, 1)

    def test_invalid_selected_dependencies_and_factories_fail_closed(self) -> None:
        cases = (
            {"broker": object(), "applier": FalseyApplier()},
            {"broker": FalseyBroker(), "applier": object()},
            {"broker_factory": object()},
            {"applier_factory": object()},
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(MutationDependencyError):
                    build_mutation_dependencies(**arguments)

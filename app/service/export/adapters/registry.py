"""Authoritative registry of downloadable package adapters."""

from types import MappingProxyType
from typing import Mapping

from app.config import ConfigValues
from app.service.export.adapters.domjudge import DOMjudgePackageAdapter
from app.service.export.adapters.icpc_2025 import ICPC2025PackageAdapter
from app.service.export.adapters.nowcoder import NowcoderPackageAdapter
from app.service.export.adapters.shared import PackageAdapter, PackageFormat
from app.service.statement.tex_compile import TexCompileService


class PackageAdapterRegistry:
    """Enumerate and resolve every package format supported by this process."""

    def __init__(
        self,
        config_values: ConfigValues,
        tex_compile_service: TexCompileService,
    ) -> None:
        domjudge: PackageAdapter = DOMjudgePackageAdapter(
            config_values,
            tex_compile_service,
        )
        icpc_2025: PackageAdapter = ICPC2025PackageAdapter(
            config_values,
            tex_compile_service,
        )
        nowcoder: PackageAdapter = NowcoderPackageAdapter()
        adapters = (domjudge, icpc_2025, nowcoder)
        by_format = {adapter.format: adapter for adapter in adapters}
        if len(by_format) != len(adapters):
            raise RuntimeError("duplicate package adapter format")
        self._adapters = adapters
        self._by_format: Mapping[PackageFormat, PackageAdapter] = MappingProxyType(
            by_format
        )

    @property
    def adapters(self) -> tuple[PackageAdapter, ...]:
        """Return adapters in stable user-facing enumeration order."""

        return self._adapters

    @property
    def formats(self) -> tuple[PackageFormat, ...]:
        """Return the registered format identifiers in the same stable order."""

        return tuple(adapter.format for adapter in self._adapters)

    def supports(self, package_format: str) -> bool:
        return package_format in self._by_format

    def require_format(self, package_format: str) -> PackageFormat:
        if package_format not in self._by_format:
            raise ValueError(f"unsupported package format: {package_format}")
        return package_format

    def require(self, package_format: str) -> PackageAdapter:
        return self._by_format[self.require_format(package_format)]

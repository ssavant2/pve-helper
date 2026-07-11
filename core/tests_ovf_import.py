from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from core.services.ovf_import import OvfImportError, parse_ovf_package
from core.services.proxmox import ProxmoxTaskResult
from core.services.vm_register import import_ovf_package_as_vm


OVF = """<?xml version="1.0"?>
<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1" xmlns:rasd="http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData">
  <References><File ovf:id="file1" ovf:href="boot.vmdk"/><File ovf:id="file2" ovf:href="data.vmdk"/></References>
  <DiskSection><Disk ovf:diskId="disk1" ovf:fileRef="file1" ovf:capacity="8" ovf:capacityAllocationUnits="byte * 2^30"/><Disk ovf:diskId="disk2" ovf:fileRef="file2" ovf:capacity="4" ovf:capacityAllocationUnits="byte * 2^30"/></DiskSection>
  <VirtualSystem ovf:id="test"><Name>Test appliance</Name><OperatingSystemSection ovf:id="ubuntu64Guest"/><VirtualHardwareSection>
    <Item><rasd:ResourceType>3</rasd:ResourceType><rasd:VirtualQuantity>4</rasd:VirtualQuantity></Item>
    <Item><rasd:ResourceType>4</rasd:ResourceType><rasd:VirtualQuantity>4096</rasd:VirtualQuantity><rasd:AllocationUnits>byte * 2^20</rasd:AllocationUnits></Item>
    <Item><rasd:ResourceType>17</rasd:ResourceType><rasd:HostResource>ovf:/disk/disk1</rasd:HostResource></Item>
    <Item><rasd:ResourceType>17</rasd:ResourceType><rasd:HostResource>ovf:/disk/disk2</rasd:HostResource></Item>
    <Item><rasd:ResourceType>10</rasd:ResourceType><rasd:Connection>Production</rasd:Connection><rasd:ResourceSubType>VmxNet3</rasd:ResourceSubType></Item>
  </VirtualHardwareSection></VirtualSystem>
</Envelope>"""


class OvfImportTests(SimpleTestCase):
    def _storage(self, root: Path):
        return SimpleNamespace(storage_id="import-store", path=str(root))

    def _write_ovf(self, root: Path, *, metadata: str = OVF) -> Path:
        package = root / "packages"
        package.mkdir()
        (package / "appliance.ovf").write_text(metadata, encoding="utf-8")
        (package / "boot.vmdk").write_bytes(b"boot")
        (package / "data.vmdk").write_bytes(b"data")
        return package / "appliance.ovf"

    def test_parse_ovf_prefills_hardware_and_orders_disks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_ovf(root)
            package = parse_ovf_package(self._storage(root), "packages/appliance.ovf")

        self.assertEqual(package.name, "Test-appliance")
        self.assertEqual(package.cores, 4)
        self.assertEqual(package.memory_mib, 4096)
        self.assertEqual(package.ostype, "l26")
        self.assertEqual([disk.href for disk in package.disks], ["boot.vmdk", "data.vmdk"])
        self.assertEqual(package.nics[0].network_name, "Production")
        self.assertEqual(package.nics[0].model, "vmxnet3")

    def test_parse_rejects_external_entities(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_ovf(root, metadata='<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><Envelope/>')
            with self.assertRaisesMessage(OvfImportError, "external entities"):
                parse_ovf_package(self._storage(root), "packages/appliance.ovf")

    def test_imports_all_ovf_disks_and_removes_temporary_import_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_ovf(root)
            storage = self._storage(root)

            class Client:
                def __init__(self):
                    self.posts = []
                    self.puts = []

                def post(self, path, *, data):
                    self.posts.append((path, data))
                    return f"UPID:test:{len(self.posts)}"

                def put(self, path, *, data):
                    self.puts.append((path, data))
                    return f"UPID:test:put:{len(self.puts)}"

                def wait_for_task(self, **_kwargs):
                    return ProxmoxTaskResult(node="pve1", upid="UPID:test", status="stopped", exitstatus="OK", raw={})

            client = Client()
            with patch("core.services.vm_register._client", return_value=client):
                upids, error = import_ovf_package_as_vm(
                    "pve1",
                    {
                        "vmid": "900",
                        "name": "test-appliance",
                        "cores": "4",
                        "sockets": "1",
                        "memory": "4096",
                        "ostype": "l26",
                        "bios": "seabios",
                        "machine": "i440fx",
                        "disk_bus": "scsi",
                        "target_storage": "target",
                        "format": "qcow2",
                    },
                    source_storage=storage,
                    source_path="packages/appliance.ovf",
                )

            self.assertIsNone(error)
            self.assertEqual(len(upids), 2)
            self.assertIn("scsi0", client.posts[0][1])
            self.assertIn("import-from=import-store:import/pve-helper-", client.posts[0][1]["scsi0"])
            self.assertIn("scsi1", client.puts[0][1])
            self.assertEqual(list((root / "import").iterdir()), [])

    def test_ova_manifest_is_checked_without_extracting_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ova = root / "appliance.ova"
            metadata = OVF.replace("boot.vmdk", "disk.vmdk").replace('ovf:id="file2" ovf:href="data.vmdk"', 'ovf:id="file2" ovf:href="disk.vmdk"')
            # This small package has duplicate disk references; it is enough to
            # exercise archive parsing and manifest verification.
            disk = b"disk"
            manifest = f"SHA1(disk.vmdk)= {hashlib.sha1(disk).hexdigest()}\n"
            with tarfile.open(ova, "w") as archive:
                for name, payload in (("appliance.ovf", metadata.encode()), ("disk.vmdk", disk), ("appliance.mf", manifest.encode())):
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
            package = parse_ovf_package(self._storage(root), "appliance.ova", validate_manifest=True)

        self.assertEqual(package.kind, "ova")
        self.assertTrue(package.manifest_present)

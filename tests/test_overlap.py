import nose
import cle
import os


class MockBackend(cle.backends.Backend):
    def __init__(self, linked_base, size, **kwargs):
        super(MockBackend, self).__init__('/dev/zero', **kwargs)
        self.mapped_base = self.linked_base = linked_base
        self.size = size

    def get_max_addr(self):
        return self.mapped_base + self.size


def test_overlap():
    filename = os.path.join(os.path.dirname(os.path.realpath(__file__)), '../../binaries/tests/i386/manysum')
    ld = cle.Loader(filename, auto_load_libs=False)
    nose.tools.assert_equal(ld.main_bin.linked_base, 0x8048000)
    nose.tools.assert_equal(ld.main_bin.get_min_addr(), 0x8048000)

    obj1 = MockBackend(0x8047000, 0x2000, custom_arch=ld.main_bin.arch)
    obj2 = MockBackend(0x8047000, 0x1000, custom_arch=ld.main_bin.arch)

    ld.add_object(obj1)
    ld.add_object(obj2)

    nose.tools.assert_equal(obj2.mapped_base, 0x8047000)
    nose.tools.assert_greater(obj1.mapped_base, 0x8048000)

if __name__ == '__main__':
    test_overlap()

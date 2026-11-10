# -*- coding: utf-8 -*-
# Build all 17 shipyard modules into .so via Cython.  Usage: python3 build_v116.py
# V1.39: + safety_user_grant (safety "normal user" roster).
# ASCII-only (safe for GBK SSH terminal).
#
# 2026-10-10 FIX (V1.16.44) -- TWO changes, both required:
#
# [1] always_allow_keywords=True   <-- THE REAL BUG FIX.
#     Flask dispatches views with keyword args:  view_functions[endpoint](**view_args)
#     With this directive OFF (Cython 0.29 default) Cython compiles 0-arg
#     functions to METH_NOARGS and 1-arg functions to METH_O, and METH_O
#     REJECTS keywords:   TypeError: board() takes no keyword arguments
#     => /board/<int:ship_id> and /detail/<int:rid> return 500,
#        while / , /boards , /sixs (zero args) keep working.
#     Valid on Cython 0.29 (default False) and 3.x (default True).
#
# [2] delete old .c/.so first, else distutils says "up to date" and
#     SILENTLY SKIPS -> you see BUILD DONE but the .so is unchanged.
#     Unlinking a .so is SAFE for a running process (it keeps its own
#     inode); OVERWRITING it in place corrupts a live process.
import glob
import os
import sys

import Cython
from setuptools import setup, Extension
from Cython.Build import cythonize

ALL_MODULES = ["app", "db", "config", "monitor", "chz_push", "holidays",
               "init_db", "sync_excel", "safety_review", "admin_grant",
               "debug_admin_grant", "debug_sup_grant", "debug_user_grant",
               "safety_user_grant", "chz_org", "sixs_data",
               "board_categories",   # V1.46 看板类别配置（漏了会 ImportError）
               "ship_overview"]      # V1.48 综合数据总览只读聚合模块

DIRECTIVES = {"always_allow_keywords": True}

print("BUILD %d MODULES" % len(ALL_MODULES))
print("CYTHON %s  DIRECTIVES %s" % (Cython.__version__, DIRECTIVES))
sys.stdout.flush()

for _m in ALL_MODULES:
    for _p in sorted(set(glob.glob(_m + ".c")
                      + glob.glob(_m + ".*.so")
                      + glob.glob(_m + ".so"))):
        try:
            os.remove(_p)
            print("  removed %s" % _p)
        except Exception as _e:
            print("  skip %s (%s)" % (_p, _e))
sys.stdout.flush()

exts = [Extension(m, [m + ".py"]) for m in ALL_MODULES]
setup(
    name="shipyard",
    ext_modules=cythonize(exts, language_level=3,
                          compiler_directives=DIRECTIVES),
    script_args=["build_ext", "--inplace"],
)
print("BUILD DONE")

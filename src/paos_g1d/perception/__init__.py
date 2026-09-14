"""Continuous camera capture for Agent-side vision, without detection or 3D localization.

Importing this package does not connect to hardware or start capture. No detector,
depth source or camera calibration is available, so this module only keeps fresh
native camera JPEGs on the shared filesystem for the Agent's own vision model.
"""

"""Native motion for the menu-bar glyph, independent of menu-row rendering."""

from __future__ import annotations

import math

import objc
from AppKit import (
    NSColor,
    NSCompositingOperationSourceIn,
    NSCompositingOperationSourceOver,
    NSImage,
    NSRectFillUsingOperation,
    NSView,
    NSViewMaxXMargin,
    NSViewMaxYMargin,
    NSViewMinXMargin,
    NSViewMinYMargin,
)
from Quartz import (
    CABasicAnimation,
    CALayer,
    CAKeyframeAnimation,
    CAMediaTimingFunction,
    CATransaction,
    CGImageGetWidth,
    kCAAnimationLinear,
    kCAMediaTimingFunctionLinear,
)


class StatusIconView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(StatusIconView, self).initWithFrame_(frame)
        if self is not None:
            self.source_image = None
            self.setWantsLayer_(True)
            self.layer().setMasksToBounds_(True)
            # AppKit owns the backing layer and uses a corner anchor. Animate
            # only this independently owned, centered child layer.
            self.glyph_layer = CALayer.layer()
            CATransaction.begin()
            CATransaction.setDisableActions_(True)
            try:
                self.glyph_layer.setBounds_(((0, 0), (18, 18)))
                self.glyph_layer.setAnchorPoint_((0.5, 0.5))
                self.glyph_layer.setPosition_((9, 9))
                self.layer().addSublayer_(self.glyph_layer)
            finally:
                CATransaction.commit()
        return self

    def hitTest_(self, _point):
        # The standard status button must retain clicks and menu tracking.
        return None

    def viewDidChangeEffectiveAppearance(self):
        objc.super(StatusIconView, self).viewDidChangeEffectiveAppearance()
        if getattr(self, "source_image", None) is not None:
            self._update_contents()

    @objc.python_method
    def set_image(self, image):
        self.source_image = image
        self._update_contents()

    @objc.python_method
    def _update_contents(self):
        # Resolve the template color in this view's appearance, once per image
        # or appearance change; the bitmap remains unchanged during motion.
        rect = ((0, 0), (18, 18))
        tinted = NSImage.alloc().initWithSize_((18, 18))

        def draw():
            tinted.lockFocus()
            try:
                self.source_image.drawInRect_fromRect_operation_fraction_(
                    rect, rect, NSCompositingOperationSourceOver, 1.0,
                )
                NSColor.labelColor().set()
                NSRectFillUsingOperation(rect, NSCompositingOperationSourceIn)
            finally:
                tinted.unlockFocus()

        self.effectiveAppearance().performAsCurrentDrawingAppearance_(draw)
        cg_image, _ = tinted.CGImageForProposedRect_context_hints_(None, None, None)
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        try:
            self.glyph_layer.setContents_(cg_image)
            self.glyph_layer.setContentsScale_(CGImageGetWidth(cg_image) / 18)
        finally:
            CATransaction.commit()


class NativeStatusIconRenderer:
    """Keep one image and let Core Animation supply rotation or a cosine pulse."""

    def __init__(self, button, *, duration: float):
        self.button = button
        self.duration = duration
        self.view = None
        self.image = None
        self.motion = None

    def update(self, image, motion: str | None) -> None:
        if image is self.image and motion == self.motion:
            return
        self.image, self.motion = image, motion
        if motion is None:
            if self.view is not None:
                self.view.glyph_layer.removeAllAnimations()
                self.view.setHidden_(True)
            self.button.setImage_(image)
            return

        if self.view is None:
            bounds = self.button.bounds()
            self.view = StatusIconView.alloc().initWithFrame_(
                (((float(bounds.size.width) - 18) / 2,
                  (float(bounds.size.height) - 18) / 2), (18, 18))
            )
            self.view.setAutoresizingMask_(
                NSViewMinXMargin | NSViewMaxXMargin
                | NSViewMinYMargin | NSViewMaxYMargin
            )
            self.button.addSubview_(self.view)

        self.button.setImage_(None)
        self.view.set_image(image)
        self.view.setHidden_(False)
        layer = self.view.glyph_layer
        layer.removeAllAnimations()
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        try:
            layer.setOpacity_(1.0)
            layer.setValue_forKeyPath_(0.0, "transform.rotation.z")
            layer.setValue_forKeyPath_(1.0, "transform.scale")
        finally:
            CATransaction.commit()
        if motion == "spin":
            animation = CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
            animation.setFromValue_(0.0)
            animation.setToValue_(-2 * math.pi)
            animation.setTimingFunction_(
                CAMediaTimingFunction.functionWithName_(kCAMediaTimingFunctionLinear)
            )
            self._add_animation(layer, animation, "spin")
        elif motion == "pulse":
            pulse = [(1 + math.cos(2 * math.pi * n / 48)) / 2 for n in range(49)]
            for path, values in (
                ("opacity", [0.45 + 0.55 * value for value in pulse]),
                ("transform.scale", [0.82 + 0.18 * value for value in pulse]),
            ):
                animation = CAKeyframeAnimation.animationWithKeyPath_(path)
                animation.setValues_(values)
                animation.setKeyTimes_([n / 48 for n in range(49)])
                animation.setCalculationMode_(kCAAnimationLinear)
                self._add_animation(layer, animation, path)
        self.button.setNeedsDisplay_(True)

    def _add_animation(self, layer, animation, key: str) -> None:
        animation.setDuration_(self.duration)
        animation.setRepeatCount_(float("inf"))
        layer.addAnimation_forKey_(animation, key)

    def hide(self) -> None:
        if self.view is not None:
            self.view.glyph_layer.removeAllAnimations()
            self.view.setHidden_(True)
        self.image, self.motion = None, None

    def close(self) -> None:
        self.hide()
        if self.view is not None:
            self.view.removeFromSuperview()
            self.view = None

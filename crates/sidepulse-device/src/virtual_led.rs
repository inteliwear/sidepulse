//! Platform-independent pixels for the virtual status device. Window shape,
//! placement, and drawing remain responsibilities of each UI adapter.

use std::f64::consts::PI;

use crate::LedDisplayState;

pub const LED_COUNT: usize = 8;
pub const BLEND_RADIUS_LEDS: f64 = 1.5;
pub const GAMMA: f64 = 0.86;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Color {
    pub red: f64,
    pub green: f64,
    pub blue: f64,
    pub alpha: f64,
}

impl Color {
    pub const fn new(red: f64, green: f64, blue: f64, alpha: f64) -> Self {
        Self {
            red,
            green,
            blue,
            alpha,
        }
    }

    pub const fn transparent() -> Self {
        Self::new(0.0, 0.0, 0.0, 0.0)
    }
}

pub fn status_colors(state: LedDisplayState, elapsed: f64, brightness: u8) -> [Color; LED_COUNT] {
    let scale = f64::from(brightness) / 255.0;
    match state {
        LedDisplayState::Done => [Color::new(0.0, scale, 0.4 * scale, 1.0); LED_COUNT],
        LedDisplayState::Ask => {
            let amount = 0.5 - 0.5 * (2.0 * PI * elapsed.rem_euclid(1.6) / 1.6).cos();
            [Color::new(scale * amount, 0.227 * scale * amount, 0.0, amount); LED_COUNT]
        }
        LedDisplayState::Idle => {
            let amount = 0.5 - 0.5 * (2.0 * PI * elapsed.rem_euclid(6.0) / 6.0).cos();
            let dim = 2.0 / 255.0;
            [Color::new(
                dim * scale * amount,
                dim * scale * amount,
                2.0 * dim * scale * amount,
                amount,
            ); LED_COUNT]
        }
        LedDisplayState::Working => {
            let cycle = 0.76 + 0.095 * (LED_COUNT - 1) as f64;
            std::array::from_fn(|index| {
                let local = elapsed.rem_euclid(cycle) - index as f64 * 0.095;
                let amount = if (0.0..=0.76).contains(&local) {
                    (PI * local / 0.76).sin().powi(2)
                } else {
                    0.0
                };
                Color::new(0.0, 0.898 * scale * amount, scale * amount, amount)
            })
        }
    }
}

pub fn battery_colors(percent: f64, brightness: u8) -> [Color; LED_COUNT] {
    let scale = f64::from(brightness) / 255.0;
    let filled = (percent * LED_COUNT as f64 / 100.0).clamp(0.0, LED_COUNT as f64);
    let rgb = if percent <= 20.0 {
        (1.0, 0.15, 0.0)
    } else if percent <= 50.0 {
        (1.0, 0.55, 0.0)
    } else {
        (0.0, 1.0, 0.4)
    };
    std::array::from_fn(|index| {
        let amount = (filled - index as f64).clamp(0.0, 1.0);
        Color::new(
            rgb.0 * scale * amount,
            rgb.1 * scale * amount,
            rgb.2 * scale * amount,
            amount,
        )
    })
}

pub fn blended_color_at_x(colors: &[Color], x: f64, led_width: f64) -> Color {
    if led_width <= 0.0 {
        return Color::transparent();
    }
    let radius = led_width * BLEND_RADIUS_LEDS;
    let mut result = Color::transparent();
    for (index, color) in colors.iter().enumerate() {
        let center = (index as f64 + 0.5) * led_width;
        let distance = (x - center).abs();
        if distance > radius {
            continue;
        }
        let weight = 0.5 + 0.5 * (PI * distance / radius).cos();
        result.red += color.red * weight;
        result.green += color.green * weight;
        result.blue += color.blue * weight;
        result.alpha += color.alpha * weight;
    }
    Color::new(
        result.red.clamp(0.0, 1.0),
        result.green.clamp(0.0, 1.0),
        result.blue.clamp(0.0, 1.0),
        result.alpha.clamp(0.0, 1.0),
    )
}

pub fn tone_mapped(color: Color, boost: f64, alpha_scale: f64) -> Color {
    let channel = |value: f64| {
        if value <= 0.0 {
            0.0
        } else {
            (value.powf(GAMMA) * boost).min(1.0)
        }
    };
    Color::new(
        channel(color.red),
        channel(color.green),
        channel(color.blue),
        (color.alpha * alpha_scale).clamp(0.0, 1.0),
    )
}

pub fn compact_preview_program(program: &str) -> String {
    if program
        .lines()
        .any(|line| line.trim().eq_ignore_ascii_case("repeat"))
    {
        return program.to_owned();
    }
    let colors: Vec<_> = program
        .split(|character: char| character.is_whitespace() || character == ':' || character == ';')
        .filter(|token| {
            token.len() == 7
                && token.starts_with('#')
                && token[1..].bytes().all(|byte| byte.is_ascii_hexdigit())
        })
        .collect();
    let prefix = if colors.len() >= 2
        && colors
            .iter()
            .all(|color| color.eq_ignore_ascii_case("#000000"))
    {
        "#00E5FF\n"
    } else {
        ""
    };
    let stripped = program.trim_end();
    if stripped.is_empty() {
        "repeat".to_owned()
    } else {
        format!("{prefix}{stripped}\nrepeat")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_and_battery_pixels_follow_legacy_rules() {
        assert_eq!(
            status_colors(LedDisplayState::Done, 0.0, 255)[0],
            Color::new(0.0, 1.0, 0.4, 1.0)
        );
        assert_eq!(
            status_colors(LedDisplayState::Ask, 0.0, 255)[0],
            Color::transparent()
        );
        assert!((status_colors(LedDisplayState::Ask, 0.8, 255)[0].red - 1.0).abs() < 1e-12);
        assert_eq!(
            status_colors(LedDisplayState::Working, 0.0, 255)[0],
            Color::transparent()
        );
        assert!(status_colors(LedDisplayState::Working, 0.38, 255)[0].blue > 0.99);
        let battery = battery_colors(25.0, 255);
        assert_eq!(battery[0], Color::new(1.0, 0.55, 0.0, 1.0));
        assert_eq!(battery[1], battery[0]);
        assert_eq!(battery[2], Color::transparent());
    }

    #[test]
    fn virtual_led_blend_has_three_led_footprint() {
        let mut colors = [Color::transparent(); LED_COUNT];
        colors[3] = Color::new(0.0, 1.0, 0.0, 1.0);
        assert_eq!(blended_color_at_x(&colors, 35.0, 10.0).green, 1.0);
        assert!(blended_color_at_x(&colors, 25.0, 10.0).green > 0.0);
        assert!(blended_color_at_x(&colors, 45.0, 10.0).green > 0.0);
        assert!(blended_color_at_x(&colors, 20.0, 10.0).green.abs() < 1e-12);
        assert!(blended_color_at_x(&colors, 50.0, 10.0).green.abs() < 1e-12);
    }

    #[test]
    fn compact_preview_preserves_loops_and_seeds_shutdown() {
        let looping = "#00E5FF 180ms ease\nrepeat";
        assert_eq!(compact_preview_program(looping), looping);
        let shutdown = "0:#000000 75ms ease; 7:#000000 75ms ease";
        assert_eq!(
            compact_preview_program(shutdown),
            format!("#00E5FF\n{shutdown}\nrepeat")
        );
    }
}

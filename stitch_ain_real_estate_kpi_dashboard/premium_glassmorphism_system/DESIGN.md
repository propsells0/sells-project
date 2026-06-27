---
name: Premium Glassmorphism System
colors:
  surface: '#faf9ff'
  surface-dim: '#d7d9e6'
  surface-bright: '#faf9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f1f3ff'
  surface-container: '#ebedfa'
  surface-container-high: '#e5e7f4'
  surface-container-highest: '#e0e2ef'
  on-surface: '#181b24'
  on-surface-variant: '#464653'
  inverse-surface: '#2d303a'
  inverse-on-surface: '#eef0fd'
  outline: '#767685'
  outline-variant: '#c6c5d6'
  surface-tint: '#4950c7'
  primary: '#474dc5'
  on-primary: '#ffffff'
  primary-container: '#6067df'
  on-primary-container: '#fffbff'
  inverse-primary: '#bfc2ff'
  secondary: '#884f41'
  on-secondary: '#ffffff'
  secondary-container: '#ffb4a2'
  on-secondary-container: '#7a4336'
  tertiary: '#006762'
  on-tertiary: '#ffffff'
  tertiary-container: '#00837c'
  on-tertiary-container: '#f3fffd'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#e0e0ff'
  primary-fixed-dim: '#bfc2ff'
  on-primary-fixed: '#02006d'
  on-primary-fixed-variant: '#3035af'
  secondary-fixed: '#ffdad2'
  secondary-fixed-dim: '#ffb4a2'
  on-secondary-fixed: '#360e05'
  on-secondary-fixed-variant: '#6c382b'
  tertiary-fixed: '#65f8ed'
  tertiary-fixed-dim: '#40dcd1'
  on-tertiary-fixed: '#00201e'
  on-tertiary-fixed-variant: '#00504b'
  background: '#faf9ff'
  on-background: '#181b24'
  surface-variant: '#e0e2ef'
typography:
  headline-xl:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '700'
    lineHeight: '1.2'
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Inter
    fontSize: 20px
    fontWeight: '600'
    lineHeight: '1.4'
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: '1.6'
  body-sm:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: '1.5'
  label-caps:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '700'
    lineHeight: '1'
    letterSpacing: 0.05em
  data-num:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: '1'
    letterSpacing: -0.01em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  container-margin: 32px
  gutter: 24px
  card-padding: 20px
  element-gap: 12px
  unit: 4px
---

## Brand & Style

The design system is defined by an ethereal, airy aesthetic that prioritizes clarity within a data-dense environment. It targets high-end analytical platforms where professional reliability meets modern, sophisticated visual trends. 

The style is a refined interpretation of **Glassmorphism**, moving away from heavy frost towards a delicate, "crystal-clear" layering technique. By utilizing a soft light mode with translucent surfaces, the interface feels lightweight and expansive. The emotional response is one of calm control and precision, achieved through the harmony of pastel tones and generous whitespace.

## Colors

The palette is anchored by a pale lavender/blue canvas that provides a cool, serene foundation. Primary interactions use a periwinkle blue, while muted coral and soft yellow act as supporting categorical colors for data visualization. A vibrant teal is reserved for highlights, success states, and critical call-to-actions to ensure they pop against the pastel backdrop.

The glass effect is achieved through semi-transparent white fills (70-80% opacity) combined with a 20px-40px background blur. Neutral tones are tinted with the primary blue to maintain a cohesive atmospheric temperature throughout the UI.

## Typography

This design system utilizes **Inter** exclusively to maintain a utilitarian yet modern feel. The typographic hierarchy is designed to handle high information density without sacrificing legibility. 

Headlines use tight letter spacing and heavier weights to anchor sections. Body text maintains generous line heights for better scanning. A specialized "data-num" style is employed for KPI cards, ensuring that quantitative values are the most prominent elements on the page. Small labels use uppercase styling with increased tracking to act as clear navigational signposts.

## Layout & Spacing

The system follows a **fluid grid** model that adapts to large-format displays common in data monitoring. A 12-column structure is used for the main content area, allowing for modular "masonry" style dashboard layouts.

Spacing is based on a 4px baseline, but defaults to larger increments (12px, 20px, 32px) to ensure the "airy" feel requested. Margins between the glass cards should be wide (24px) to allow the background color to bleed through, reinforcing the sense of depth and separation between data modules.

## Elevation & Depth

Depth is the cornerstone of this design system. Instead of traditional dark shadows, hierarchy is established through **Glassmorphism** and soft environmental occlusion.

1.  **Base Layer:** The lavender-blue background.
2.  **Surface Layer:** White cards with 70% opacity and a `backdrop-filter: blur(24px)`.
3.  **Edge Treatment:** A 1px solid white border at 40% opacity on the top and left edges of cards creates a "specular highlight," simulating a glass edge.
4.  **Shadows:** Shadows are extremely subtle, using the primary periwinkle color instead of black (e.g., `box-shadow: 0 10px 30px rgba(124, 131, 253, 0.1)`).
5.  **Interactive States:** On hover, cards should slightly increase in opacity and shadow spread, creating a "lifting" effect.

## Shapes

The design system employs a "Rounded" shape language to soften the density of the data. Main containers and dashboard cards use a radius of 1.25rem (20px). Internal elements like buttons, input fields, and chips use a smaller 0.5rem (8px) radius to maintain a consistent but distinct hierarchy. Graphs and chart elements (like bar ends or line nodes) should always be rounded to avoid harsh intersections within the glass containers.

## Components

### Cards
Cards are the primary container. They must feature the glass blur effect. For secondary cards (nested within main containers), use a slightly darker, more transparent fill to create a "recessed" look.

### Data Visualization
Charts are the centerpiece. 
- **Donut Charts:** Use thick strokes with rounded caps. 
- **Line Charts:** Use smooth Catmull-Rom splines with subtle area fills (gradients from the line color to transparent).
- **Stacked Bars:** Use rounded corners on the top-most segment of the stack.
- **Color Logic:** Use the pastel palette sequentially (Periwinkle -> Coral -> Yellow -> Teal).

### Buttons
Primary buttons use a solid gradient of Periwinkle to a slightly lighter blue. Secondary buttons are "ghost" style with a 1px periwinkle border.

### Inputs & Selects
Input fields should be semi-transparent white with a subtle inner shadow, making them feel like they are etched into the glass surface. Use the Primary color for active focus states.

### Chips
Used for filtering or status. These should be pill-shaped and utilize the background color of the specific data point they represent at 20% opacity with 100% opacity text.
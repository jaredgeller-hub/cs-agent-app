import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ChipRow from '../src/components/chat/ChipRow';

afterEach(() => {
  vi.clearAllMocks();
});

const FIRST_RUN_CHIPS = [
  'What should I focus on first today?',
  'Show me my at-risk accounts',
  'Which renewals are coming up?',
];

describe('ChipRow — first-run helper chips', () => {
  it('renders one button per chip with the exact chip text', () => {
    render(<ChipRow chips={FIRST_RUN_CHIPS} onChipClick={vi.fn()} />);
    for (const text of FIRST_RUN_CHIPS) {
      const btn = screen.getByRole('button', { name: text });
      expect(btn).toBeInTheDocument();
      expect(btn).toHaveClass('chip');
    }
  });

  it('clicking a chip fires onChipClick with the chip text exactly once', () => {
    const onChipClick = vi.fn();
    render(<ChipRow chips={FIRST_RUN_CHIPS} onChipClick={onChipClick} />);

    const target = 'Show me my at-risk accounts';
    fireEvent.click(screen.getByRole('button', { name: target }));

    expect(onChipClick).toHaveBeenCalledTimes(1);
    expect(onChipClick).toHaveBeenCalledWith(target);
  });

  it('hides the entire row after the first tap (one-shot)', () => {
    const onChipClick = vi.fn();
    const { container } = render(
      <ChipRow chips={FIRST_RUN_CHIPS} onChipClick={onChipClick} />
    );

    // Row is present before the tap.
    expect(container.querySelector('.chip-row')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', {
      name: 'What should I focus on first today?',
    }));

    // Row is gone after the tap; component returns null.
    expect(container.querySelector('.chip-row')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('renders nothing when chips is empty or undefined', () => {
    const { container: c1 } = render(<ChipRow chips={[]} onChipClick={vi.fn()} />);
    expect(c1.querySelector('.chip-row')).toBeNull();

    const { container: c2 } = render(<ChipRow onChipClick={vi.fn()} />);
    expect(c2.querySelector('.chip-row')).toBeNull();
  });

  it('does not throw when onChipClick is undefined (optional handler)', () => {
    render(<ChipRow chips={FIRST_RUN_CHIPS} />);
    // Optional-chaining call: the click must be a no-op, not a crash.
    expect(() => {
      fireEvent.click(screen.getByRole('button', {
        name: 'Which renewals are coming up?',
      }));
    }).not.toThrow();
  });
});

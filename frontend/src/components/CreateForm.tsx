import { type ReactNode } from 'react'

export interface CreateField {
  name: string
  label: string
  required?: boolean
  placeholder?: string
}

interface CreateFormProps {
  title: string
  open: boolean
  fields: CreateField[]
  values: Record<string, string>
  submitting: boolean
  onChange: (name: string, value: string) => void
  onSubmit: () => void
  onClose: () => void
}

/** 通用登记表单面板：站点与凭证共用，字段由各页面按模块声明。 */
export function CreateForm(props: CreateFormProps) {
  if (!props.open) {
    return null
  }
  return (
    <div className="modal-mask" role="presentation" onMouseDown={props.onClose}>
      <form
        className="modal-card"
        onSubmit={(event) => {
          event.preventDefault()
          props.onSubmit()
        }}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal-head">
          <strong>{props.title}</strong>
          <button className="link" type="button" onClick={props.onClose}>关闭</button>
        </header>
        <div className="form-grid">
          {props.fields.map((field) => (
            <label key={field.name} className="form-item">
              <span>
                {field.label}
                {field.required ? <em className="required">*</em> : null}
              </span>
              <input
                value={props.values[field.name] ?? ''}
                placeholder={field.placeholder ?? `请输入${field.label}`}
                onChange={(event) => props.onChange(field.name, event.target.value)}
              />
            </label>
          ))}
        </div>
        <footer className="modal-foot">
          <button className="btn" type="button" onClick={props.onClose}>取消</button>
          <button className="btn primary" type="submit" disabled={props.submitting}>
            {props.submitting ? '提交中…' : '确认登记'}
          </button>
        </footer>
      </form>
    </div>
  )
}

export function FormHint({ children }: { children: ReactNode }) {
  return <p className="form-hint">{children}</p>
}
